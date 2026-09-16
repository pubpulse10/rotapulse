# Leave — design

**Status: agreed, not built.** No code has been written for any of this.

Agreed with Steve (owner) over 15–16 September 2026, prompted by two things: RotaPulse
records leave as a single undifferentiated "away" with no types and no balance, and a
customer asked to be able to block dates out of the holiday request system for events
needing full cover.

Build in three steps — see [Order of work](#order-of-work). Each step is usable on its own.

---

## What exists today

Worth reading before changing any of it, because several parts already work and the gaps
are narrower than they look.

**The data.** One table, `leave_request`: `person_id`, `venue_id`, `start_date`,
`end_date`, `status` (`pending` | `approved` | `declined`), `requested_at`, `decided_at`,
`decided_by_person_id`. No type, no half days, no note.

**The only counting that exists.** `app/leave.py::days_taken_count()` counts dates inside
approved leave that fall on a weekday the person normally works, taken from
`rota_staff_detail.availability` (JSON, `{"mon": true, ...}`), starting from the venue's
holiday year start (`venue_settings.holiday_year_start_date`, stored `MM-DD`). Two
properties matter:

- It stops at **today**. Leave booked for next month is not in the number.
- It counts a day when availability says that weekday is worked, **defaulting to true**.
  See [The bug to fix first](#the-bug-to-fix-first).

**What people see.** Staff get one line on their Leave page: "Holiday days taken this
year: N". Admins get `rota_grid.leave_queue` (pending requests, plus current and upcoming
approved leave), `create_leave` (records leave on someone's behalf, straight to approved),
`approve_leave`, and `decline_leave` — which doubles as "take someone off approved leave".
`rota_grid._is_on_approved_leave()` stops someone being rostered on approved leave.

**Notifications.** Admins are told when a request is submitted (`notification_settings`
event `leave_request`). **Nothing tells the staff member when it is approved or declined.**

**No entitlement exists anywhere.** There is no allowance, no balance, no report.

**Useful things already present:** `rota_staff_detail.start_date` (added by migration, so
mid-year pro-rata is possible — but see the caveat in [Allowance](#allowance));
`venue_role` and `venue_membership.job_role_id`, so blocking by staff group is
straightforward; and clocked hours in `attendance`, which is what an hours-based model
would eventually need.

---

## The five types

| Type | Reduces the holiday balance? | Who records it |
| --- | --- | --- |
| Paid leave | **Yes** — the only one that does | Staff request, admin approves |
| Unpaid leave | No | Staff request, admin approves |
| Sick | No | Admin only |
| Maternity | No | Admin only |
| Day in lieu | No — see below | Admin only |

Existing `leave_request` rows all become **paid leave** on migration. That is what they
were always meant to be.

Sick, maternity and lieu are admin-recorded because nobody requests being ill in advance.
They still appear in the staff member's own history — it is their own record, and they are
entitled to see it.

**Lieu is recorded but has no balance.** A proper lieu balance needs a ledger of days
*earned* as well as taken. The owner's pub does not use lieu at all ("if staff work they
get paid"), so the earned side is deliberately not being built. The type exists so the day
off can be recorded and reported without coming out of someone's holiday. If a multi-pub
customer ever needs the balance, the recorded days are already there and only the earning
side has to be added.

The consequence: our summary will **not** have the competitor's "+3 time in lieu" line
topping up the allowance.

---

## Half days

Copy the mechanism from the competitor's screen rather than inventing one: **each end of a
booking can be a half day**. "From the afternoon of the 16th" and "until midday on the
20th" covers the real cases — an odd afternoon off, leave starting after a lunch shift —
without a separate half-day concept.

Stored as a portion on each end of the request; counted in halves.

---

## Allowance

### The rule

Statutory holiday is **5.6 weeks a year**, not 28 days. 28 is what 5.6 weeks comes to for
somebody working five days a week — a cap, not the entitlement. Because it is defined in
weeks, **a "day" of leave is one of that person's normal working days, whatever length it
is**: two hours for a cleaner, six or seven for bar staff.

So the venue sets a **full-time allowance** (default 28 days) and **full-time days per
week** (default 5), and each person's allowance is:

```
allowance = full_time_allowance x (their usual days per week / full_time_days_per_week)
            capped at full_time_allowance
```

For the 28/5 default that is exactly 5.6 weeks. It also generalises: a venue that gives 30
days to full-timers pro-rates correctly for everyone else.

"Usual days per week" comes from the availability already on the staff record.

**A flat 28 for everyone would be wrong.** A cleaner working two days a week would get 28
days off, roughly two and a half times the 11.2 days their pattern earns. The owner
originally asked for a flat 28; this rule is what he agreed to instead.

### Mid-year starters

Pro-rata by the share of the holiday year remaining from `rota_staff_detail.start_date`,
rounded to the nearest half day.

**Caveat:** `start_date` is only filled in for staff added through the admin create form.
Anyone who joined through an invite link has it blank. Where it is blank, use the full
allowance and **say so on screen** rather than silently guessing. Add the start date to the
invite flow so it stops happening.

### Editable, and stays edited

Every calculated allowance is editable. A record that has been edited by hand is marked as
such, so a later recalculation (a changed availability, say) never quietly overwrites a
figure the landlord chose.

### The below-statutory warning

Show a warning when a person's allowance is below **5.6 × their usual days per week**
(capped at 28). Being more generous is always the landlord's call; being accidentally under
is the one that causes trouble.

---

## Usual daily hours

Days alone are no use to payroll — a day off has to be worth an amount of time.

Each staff member gets a **usual daily hours** figure: 2 for the cleaner, 6 or 7 for bar
staff. The app suggests it from their recent clocked shifts and the landlord can edit it,
the same pattern as carry-over.

---

## Fixed pattern versus irregular hours

These are two different models, and the distinction is **per person, not per venue** — a
pub can easily have a fixed-pattern cleaner and bar staff whose shifts vary.

- **Fixed pattern.** The allowance is set at the start of the year. This is what gets built.
- **Irregular hours.** Since April 2024, entitlement builds up as they work, at 12.07% of
  hours worked. You cannot show that person "allowance 28, remaining 22" in January,
  because they have not earned it yet. **Not being built now.**

Each staff record carries a **holiday basis** of `fixed` (built) or `accrual` (not built).

### The decision that matters today

**Every leave record stores its hours value as well as its days, from the first version.**

It costs nothing now. It is the difference between adding the accrual model later as a new
feature, versus migrating years of historical leave that never captured hours. The owner
explicitly asked that customers operating differently from his own pub be factored in, and
this is how that gets factored in.

### Freeze the figures at approval

The days and hours a leave record is worth are **calculated once, when it is approved**, and
stored on the record. They are only recalculated if someone edits the dates or portions.

Without this, changing a person's availability or usual daily hours would silently rewrite
last year's history. For a number staff argue about, history has to hold still.

---

## Carry-over

**Not automatic**, by the owner's decision. An automatic year-end rollover writes numbers
into people's records unattended, and the real figure often differs from the raw remainder:
The Cock shuts the first week in January and requires staff to use up what is left, having
given six weeks' statutory notice. Some leave gets used, some may be forfeited.

Instead: a **carried-over box** on each staff record for the current holiday year, with last
year's remaining shown beside it as a suggestion to accept or overtype. No cap — the
landlord types what they have agreed.

---

## The two reports

### 1. Per-person position

The screen the owner asked for, modelled on the competitor summary he supplied.

```
Year end 31 December
  Allowance            25.0
  Carried over          3.0
  ------------------------
  Total                28.0
  Paid leave taken     15.0
  Booked ahead          4.0
  ------------------------
  Remaining             9.0

Also taken this year:  Sick 3.0   Unpaid 1.0   Maternity 0   Lieu 0
```

Three differences from the competitor's version, all deliberate:

- **Taken and booked ahead are separate.** Their single "annual leave 25" almost certainly
  includes leave booked but not yet taken. Ours splits them, because "I have had 15 days"
  and "I have committed 19 of my 28" are different conversations.
- **No donut chart.** Theirs shows the same "6 days" as the pink bar beside it. The space
  goes to the by-type breakdown instead, which their card does not have at all.
- **The other four types are shown**, not just annual leave.

Staff see their own. Admins see anyone's, from the staff record and from the report below.

### 2. All-staff summary, any date range

Dates can look backwards or forwards. One row per staff member, one column per type in
days, plus a total. CSV and PDF export, following the payroll report's existing pattern.
Admin only.

---

## Approval notification

When leave is approved or declined, **tell the staff member**. The app already notifies
admins when a request comes in and tells staff nothing about the outcome, so they have to
go and look.

New notification event alongside the existing ones, so it respects the venue's notification
settings. Email by default. If it is ever sent by SMS, keep the message inside GSM-7 — see
commit `6edf40d`; an em-dash doubles the cost of every message.

Cancelling already-approved leave should notify too. That is the same route
(`decline_leave`), so it comes free if the notification hangs off the status change rather
than the button.

---

## Blocked dates

Dates the landlord needs full cover on, which staff cannot request leave against.

- **Venue-level**, with a note of **20 characters maximum** ("Beer festival", "Village
  carnival").
- **Scoped by staff group** via tick boxes per role — Bar staff, Kitchen staff. No roles
  ticked means the whole venue. Staff with no role assigned are affected only by
  venue-wide blocks, and the form says so when a role-specific block is created.
- **No annual repeat** — the owner does not want one. A recurring event is re-entered.
- **Staff cannot request leave** covering a blocked date, and are told why, showing the
  note.
- **Blocked dates are listed on the request form** before they pick. Being refused after
  the fact is annoying; seeing "these dates are unavailable" first is not.
- **A partly-overlapping request names the clashing dates** rather than refusing the whole
  booking.
- **The admin can still record leave over a blocked date**, with a warning. There are
  always exceptions and the block must not handcuff the landlord.
- **Not applied to sick or maternity.** You cannot block someone being ill. (Both are
  admin-recorded anyway.)
- **Shown on the rota grid** with the note, so the day explains itself.

---

## Bulk leave

"Add leave for everyone between these dates", on the admin leave screen. Type selectable,
usually paid.

This exists because of how The Cock actually runs: shutting the first week in January and
putting everyone on leave currently means adding it person by person. It skips anyone
already on leave for those dates and reports what it created.

---

## Payroll

Paid holiday is **currently missing from the payroll report entirely** — the report counts
clocked hours only. Now that the pub has cancelled RotaCloud and relies on this report, that
is a real gap.

- Show **paid leave days and hours** for the period, per person, beside worked hours.
- **Do not add it to the gross pay total, and do not calculate holiday pay.** For variable
  hours that is a 52-week average, and it belongs with payroll. RotaPulse hands over the
  days and the hours; payroll applies the rate.
- Per-person **"holiday pay rolled up"** flag. Some pubs pay irregular-hours staff their
  holiday as a percentage on every payslip. For those people the report must not also
  prompt for holiday pay, or they get paid twice. The flag keeps the time-off tracking and
  drops the prompt.

---

## The bug to fix first

`days_taken_count()` decides a date counts as a leave day using
`availability.get(weekday, True)` — **defaulting to true**. A staff member whose
availability has never been set has every day treated as a working day, so a week's holiday
counts as 7 days instead of 5.

Nobody notices today, because the number is informational. The moment it becomes "allowance
minus taken equals remaining", it is an argument with a staff member about their holiday.

Fix: where availability is not set, say so and prompt for it, rather than guessing. Note
that fixing this **changes the number staff currently see**, so it is worth doing in the
first step, before anyone treats the figure as a balance.

---

## Deliberately not building

Recorded so nobody adds them later thinking they were forgotten:

- **Holiday pay calculation** (the 52-week average). Payroll's job, not a rota app's.
- **The lieu earned ledger.** The type is recorded; the balance is not.
- **The 12.07% hours-accrual model** for irregular-hours staff. Designed for, not built.
- **Automatic year-end carry-over.** Owner's decision.
- **Annual repeat on blocked dates.** Owner's decision.

---

## Data model

New columns and tables. Names are a starting point, not a contract.

**`leave_request`** gains:

| Column | Purpose |
| --- | --- |
| `leave_type` | `paid` \| `unpaid` \| `sick` \| `maternity` \| `lieu`, default `paid` |
| `start_portion`, `end_portion` | `full` \| `half` — the half-day mechanism |
| `days_counted`, `hours_counted` | Frozen at approval; recalculated only on a date edit |
| `note` | Free text, admin-visible |

**`rota_staff_detail`** gains: `allowance_days`, `allowance_is_manual`,
`usual_daily_hours`, `holiday_basis` (`fixed` \| `accrual`), `holiday_pay_rolled_up`.

**`venue_settings`** gains: `full_time_allowance_days` (default 28),
`full_time_days_per_week` (default 5).

**`leave_carry_over`** — new: `venue_membership_id`, `year_start_date`, `days`,
`updated_at`. One row per person per holiday year, so history survives.

**`blocked_date`** — new: `venue_id`, `start_date`, `end_date`, `note` (≤20 chars),
`created_at`. With **`blocked_date_role`**: `blocked_date_id`, `venue_role_id`. No rows
means venue-wide.

---

## Order of work

**1. Types, half days, and the approval notification.** All on the same screens. Existing
leave becomes paid leave. Fix the availability bug here, before the number means money.

**2. Allowances and the reports.** Venue full-time allowance and days per week, per-person
allowance with pro-rata and the statutory warning, usual daily hours, carry-over, the
per-person position, the all-staff date-ranged report with exports, and paid leave on the
payroll report.

**3. Blocked dates and the bulk leave button.**

---

## Open questions

- **Sick records and data protection.** Sick leave is health information, which carries
  extra protection. The privacy policy does not currently mention that RotaPulse holds it,
  and there is no retention rule. Worth sorting before a lot of it accumulates.
- **Leavers.** Somebody leaving mid-year has accrued only part of their allowance, and
  untaken holiday is usually paid off. The report should show the position at the leaving
  date; calculating what is owed is payroll's job. Not yet discussed with the owner.
- **Staff-visible history.** Assumed: each person sees their own leave including sick, and
  nobody else's. Not explicitly confirmed.

---

## A note on the law

The entitlement rules above (5.6 weeks, the 28-day cap, pro-rata by working pattern, the
12.07% accrual for irregular-hours workers from April 2024, and rolled-up holiday pay for
that same group) are the author's understanding, not legal advice. Gov.uk publishes a
holiday entitlement calculator. Anything here that drives money should be confirmed with
whoever runs the venue's payroll before it is relied on.
