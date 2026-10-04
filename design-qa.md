# Telegram day plan — native implementation QA

Selected source: `C:/Users/volob/.codex/generated_images/01a04cec-5705-7220-9628-ac261869e982/exec-2d1d850f-4117-4317-9e00-03bf39757341.png` (second displayed ideation result).
Implementation screenshot: `design/telegram-day-plan-desktop-2026-10-04.png`.
Combined comparison: `design/day-plan-qa-comparison.png`; both source and actual were opened together in this image, not compared from memory.

Scope: actual Telegram rich-message implementation, not a separate website. Target state: Sunday 4 October 2026, next teaching day Tuesday 6 October, four lessons; other days closed; dark theme. Telegram owns fonts, colors, cell sizing and native icons. Source pixels 851×1844; native message crop 595×830 from a 1872×1080 desktop capture. Comparison normalizes each content region to width 390 without stretching height. This is a hierarchy/native-component comparison, **not** proof of a 390 CSS-pixel Android viewport or pixel-exact clone. A subsequent real narrow desktop window was observed at 558×849 pixels. Actual Android/iOS and large-font themes are not available in this session and remain unverified.

## Fidelity checks

- Typography: native Telegram font; bold subjects and start times, readable teacher on its own line. No source data is cropped or abbreviated by our code. Long Information Modelling subject wraps in the narrow client. Smaller type/denser rhythm than the illustration is an intentional consequence of the client's font settings, not a custom font substitution.
- Layout: same two-column time/information hierarchy as source. No repeat header row; time centered, content left aligned; full four-entry Tuesday visible in the desktop capture. On the narrower window vertical scrolling remains necessary; do not claim universal one-screen fit.
- Colors: client dark navy/graphite, no marker fill and no colored table header cells. No invented CSS gradients or custom cell colors. Sender and calendar link use client-owned theme colors.
- Images/icons: no custom raster assets are required. Header/calendar/disclosure affordances are existing native Telegram elements, not approximated artwork.
- Copy: source-supplied dates, subjects, rooms/buildings and teacher initials preserved. Full teacher names are retained when supplied. Dead 'next week' disclosure intentionally omitted because data only runs through 11 October. Status and source-check timestamps are separate by design.

## Comparison history

Iteration 1 (commit 71e70f8): main-day table and closed overview meet the selected hierarchy. [P2] Opening 'Other days' renders all remaining tables at once; narrow Telegram keeps the viewport near the end of the enlarged message, showing Friday first. This was observed in the actual client after opening the disclosure. Fix: inside the existing closed overview show closed date-only day disclosures first, then expand only the requested table. Trade-off: other days take two taps (overview + day), while the useful main day always takes zero. No color dependence or new server interaction.

Iteration 2 (34ab3fd): compact date-only overview delivered to the same message. Run
37192755573 succeeded. The final typography build retained this behavior; native
558×849 evidence in `design/telegram-day-plan-other-days-verified.png` shows three
closed dates, not all tables. Wednesday was opened separately and closed again;
the outer overview was then closed. The previous P2 jump to the last of all
remaining tables is resolved by limiting the expansion to the selected day.

Iteration 3 (dc6bc1f): the user explicitly requested typography refinement while
retaining variant 2. Source visual truth for this scoped polish is the accepted
native baseline `design/telegram-typography-before.png`; implementation is
`design/telegram-typography-after.png`. Both are 558×849 native Desktop pixels,
dark theme, same Tuesday 6 October four-lesson table and closed other-day state,
same scroll position. Full combined input `design/typography-full-comparison.png`
(1126×849) and focused input `design/typography-table-comparison.png` (886×419)
were opened and reviewed together, at 1:1 scale, with a 10 px gap between panels.
Focused source/implementation regions are x=85, y=161, w=438, h=419. No CSS
viewport or deviceScaleFactor applies to this native client capture.

Five fidelity surfaces rechecked: native font and sizing retained, subjects/start
times bold, type italic, teachers regular; short initials have consistent spaces
without shortening names. Row heights and all borders remain unchanged in the
compared four-row state; time center/middle, content left/top. The long final
subject now wraps before 'в строительстве', rather than stranding the preposition
at the end of the previous line. Theme colors and contrast are unchanged, with
no marker fill. No images/assets were introduced or replaced. Copy and source
values are retained except display whitespace; source freshness changes naturally
from 12:37 to 12:50 and is not a design mismatch. No actionable P0/P1/P2 finding.
Full initial mock/native evidence remains above; this is an intentional native
adaptation, not a pixel-identical clone of the illustration.

`design/telegram-typography-wednesday.png` confirms a separate three-entry day
with two teachers in one entry and building К303/К; no lost teacher or clipped
time. The actual source only gives initials, which were not invented into names.

## Implementation checklist

- Delivered through quiet CI: GitVerse 1722413 (success, 50 s), GitHub 37193410037
  send job 111410175672 and deploy-calendar 111410273038 both success.
- Send log at 09:50:33 UTC confirms same message_id=15396; native topic still has
  one message, not a duplicate publication.
- Compact overview and individual Wednesday open/close verified; returned to the
  closed overview.
- Captured post-fix evidence and finalized this report. All 351 tests, Ruff and
  strict mypy (67 source files) passed.

## Residual test gaps

Iteration 4 (a8c3730): user requested a stable short-subject dictionary and glossary
in the bot description instead of any new card disclosure. Native 558×849 evidence
`design/telegram-subject-aliases-after.png` shows БЖД, ВиВ, Строймеханика and
Инф. моделирование, all on one line in the observed Tuesday table. Subject aliases
are display-only; teachers, lesson identity, diff inputs and ICS remain intact.
Telegram controls automatic column sizing: the time column grew after the titles
became shorter, but content remains readable and unclipped. No claim of universal
one-screen fit or Android/iOS verification. No marker fill or additional UI blocks.
`design/telegram-subject-glossary-profile.png` shows both acronym expansions on
the actual bot profile. The full 512-character description contains all five
mappings, verified by CI readback; the 120-character profile contains БЖД/ВиВ.
GitHub profile run 37194703346, tests 37194703339, and delivery/calendar run
37194781969 all succeeded. Native topic still contains one message, refreshed at
13:15 MSK. 362 local tests, Ruff and strict mypy for 69 source files passed.

Android/iOS rendering, arbitrary custom themes, large text, and actual ticking cadence of the native relative date entity are not directly verified. Automated tests cover timestamps and boundaries; the Sunday live card has no countdown to test visually. Main CI delivery and phone calendar deployment succeeded on run 37192401639 with message_id=15396.

final result: passed
