# CQA Hackathon — research: idea options and tradeoffs

> Research done 2026-11-07. Inputs: [task.md](task.md), [../plan.md](../plan.md), [checklist.md](checklist.md), [criteria.md](criteria.md), [../data/checklists/default.json](../data/checklists/default.json), all 60 DEV chats and 5 calls in [../data/dev/](../data/dev/index.md), and web lookups on four topics (sources at the end).
>
> **How reliable this is.** The findings from the DEV data were checked by reading every transcript. The web findings mostly come from search summaries and vendor help pages; papers were **not** read in full. Unverified items are marked.

---

## 0. TL;DR — recommended path

| Area | Recommendation | Main alternative |
|---|---|---|
| Scoring | One LLM call per criterion, prompt generated from the checklist JSON, 3 runs with majority vote, disagreement shown as "check manually". Code computes all points. | One call for all criteria, single run (cheaper, faster, less accurate) |
| Extra features ("fichas") | (1) Before/after comparison when the checklist changes; (2) resolved appeals saved as precedents and fed into the prompt | Dashboard across the dataset; CSV export |
| Local LLM | Not the main path. Add an Ollama provider switch only if there is spare time around hour 18. | Full hybrid with routing |
| Ambiguous rules | Write the resolutions from §4 into `global_rules` (data, not code) | Keep the literal reading |
| Biggest accuracy risk | Reading criterion 07 literally: it would turn ~35 of 60 DEV chats into Нет | — |

---

## 1. Scoring architecture options

### 1a. How to split the calls

| Option | How it works | Pros | Cons |
|---|---|---|---|
| **A. One call, all criteria** | A single prompt holds every criterion and returns one JSON array | Cheapest (~1 call per dialogue); simplest; easy to state the cost | One criterion's reasoning bleeds into another, e.g. an insult (08) dragging down 05/06. A preprint (RUVERBENCH, unverified) reports that bundling 4–5 rubrics in one call lowers accuracy for every model tested |
| **B. One call per criterion** *(recommended)* | Each prompt = global rules + one criterion copied verbatim + numbered transcript | Each judgment stays separate; easy to see which criterion is wrong; a changed or new criterion affects only its own call, which makes the change-a-criterion demo clean | ~8× the calls. Cache the shared transcript prefix to offset this; latency is fine if calls run in parallel |
| **C. Grouped by section** | One call per section from `sections[]` (start / safety / conduct / critical) | Middle cost; groups criteria that depend on each other (03+04) | Grouping is driven by the data, and it breaks if organizers move criteria between sections |
| **D. Decomposed yes/no sub-questions (CheckEval/TICK style)** | The LLM first splits each criterion into atomic yes/no checks, which are then answered | Most transparent; small models do well (RocketEval: Gemma-2-2B had 0.965 correlation with human preferences) | More moving parts, and generating the sub-questions is itself a source of variance. Too much for 24 h, except maybe as the local-model path |

### 1b. Stability: one run or several

| Option | Pros | Cons |
|---|---|---|
| Single run, temperature 0 | Cheapest, deterministic-ish | No signal of how uncertain the model is |
| Single run + model-reported `confidence` | Free | Models report confidence poorly; in practice it is mostly high |
| **3 runs, majority vote** *(recommended)* | Smooths out flip-flops on borderline cases; disagreement between runs is a real uncertainty signal, which doubles as a UI feature | 3× cost (still cents per dialogue with caching) |
| Adaptive: one run, plus 2 more only when the first is unsure or the type is scale | Saves cost | More logic; "unsure" must be defined |

### 1c. Prompt order inside each call

1. **Applicability first** (only if `na_allowed`): is the `na_condition` met? This keeps the model from forcing "Нет" on criteria that don't apply.
2. **Reasoning**, citing the numbered lines (`#10`).
3. **Evidence line numbers** (`evidence_idx[]`). Validate that they exist and belong to the expected speaker.
4. **Answer** from the allowed values, enforced by a JSON schema.

Putting reasoning before the answer is common practitioner advice; I found no primary source for it, so check it on DEV (§6).

### 1d. Keeping the prompt 100% generated from the checklist (hard blocker)

- The template may only loop over `criteria[]`, `global_rules[]`, `scale[]` and `na_condition`. The criterion text is copied in verbatim.
- **Allowed:** code-computed general facts about the transcript that mention no criterion: index of the first operator line, the client's language, gaps between client and operator lines, the last system event, whether an "identity confirmed" event exists. Give these to the model as a "Transcript facts" block for every criterion.
- **Not allowed:** `if criterion.id == "c07"`, few-shot examples copied from DEV, any per-conversation rule.
- Proof for judges: a test that loads a modified checklist (rename, new criterion, binary → scale) and checks that the generated prompt changes and nothing breaks.

---

## 2. Differentiating features ("fichas")

Ranked by value for the judges per hour of work.

| # | Feature | Why it scores | Effort |
|---|---|---|---|
| 1 | **Before/after comparison when the checklist changes**: re-run, then a table of changed answers and totals with the new explanation | Directly proves "process correctness", which is a pass/fail criterion; fits the 4th minute of the demo | ~2–3 h (data is already versioned) |
| 2 | **Resolved appeals become precedents**: a decided appeal (criterion, excerpt, final answer, reason) is stored and injected as an example into future prompts for that criterion | Shows the human review loop improving the system. The QA-vendor research found **no confirmed** vendor that feeds overrides back to the AI (only a Kaizo design blog suggests using disputed cases for calibration), so this stands out. It's data, not code, so it doesn't count as hardcoding | ~2 h. Risk: a bad precedent spreads, so add a toggle and show which precedents were used |
| 3 | **"Check manually" flag** from runs that disagree, plus a review queue sorted by uncertainty | Almost free with 3 runs; matches the plan's framing that the LLM is a fast first reviewer, not the judge | ~1 h |
| 4 | **Calibration view**: the user's labels against AI output, with agreement per criterion | It's the eval.py you need anyway, put in the UI; mirrors Zendesk QA "calibration" | ~2 h if eval.py already exists |
| 5 | Dashboard across the dataset (average, per criterion, per language, top violations) | Looks good, low risk | ~2 h |
| 6 | Provider switch with cost and latency shown per evaluation | Covers the "diversity" bonus and the cost question in the FAQ | ~1–2 h, plus local-model setup |

Small appeal-UX details from vendor research worth copying (cheap):
- Per-criterion appeal with a required comment and an optional **proposed score** (Zendesk QA).
- Decision with a reason from a dropdown ("Grader error" / "Not grader error" / "Checklist wording issue", like MaestroQA).
- Keep the original score; record the decision and reason next to it (Kaizo: "the audit trail is the point").
- States: submitted → in review → accepted / rejected. The same score can be appealed again.
- The reviewer sees the original answer, the comment, and the dialogue fragment with the evidence lines highlighted.

---

## 3. Hybrid local + cloud LLM

| Option | Pros | Cons |
|---|---|---|
| **Cloud only** | Best accuracy, best Kazakh; least time spent | No diversity bonus |
| **Cloud main + local switch** *(recommended only if time allows)* | Covers the bonus; one shared provider interface; report local accuracy honestly next to cloud | Setup time; local results will probably be worse |
| Local main | Strongly "different"; zero API cost | Accuracy is the dominant criterion; small models are likely weak on Kazakh (my inference, not measured) and on the subtle Нет/N/A cases |
| Hybrid routing (local first, cloud for uncertain cases) | Interesting technically | Most complex; two models to tune in 24 h |

Verdict: diversity is explicitly a **bonus**, and [criteria.md](criteria.md) says accuracy is the main measure. Don't let local work touch the critical path. If you add it, the decomposed yes/no format (option D) is where small models do best (RocketEval).

---

## 4. Ambiguous checklist rules: resolutions grounded in DEV

Every resolution below can go into `global_rules`, where it survives a criterion being replaced.

### 01 Contact
- **Evidence:** `p1-ru-014` "Привет, где именно ищете?" (no name). `p1-kz-008` "Сәлем." (no name). `p1-en-015` no greeting at all.
- **Resolution:** Нет for all three. "Hi, I'm Leo/Chris" = Да (casual is fine if a name is given). On calls a masked name "[Имя скрыто]" counts as introducing oneself (`p4-ru-002`).
- **Gap:** no DEV chat has the operator answering in a different language from the client. The control set may test it. Rule: compare against the language of the client's last message before the operator's first reply. Russian UI labels quoted inside a KZ/EN reply (`p1-kz-002`, `p1-en-003` «Кабинет») don't count as switching language.

### 02 Needs: Да / Нет / N/A
- **Нет:** asking about something the client already stated (`p1-ru-014`, `p1-kz-008` "Жоғарыда жаздым ғой"); ignoring the stated question (`p1-ru-026` "Чем могу помочь?"); answering blindly without asking about the device (`p1-ru-016` "Переустановите приложение", `p1-en-015`).
- **N/A:** the request was clear and the operator answered it directly (`p1-ru-001`, most simple chats).
- **Да:** the operator asked only what was missing (`p1-ru-002`, `p1-ru-017`, `p1-ru-030`).

### 03 Identification
- **Нет:** account data revealed before verification (`p1-ru-009`, `p1-kz-015`, `p1-en-014`; the client even objects).
- **Да:** a system event "Личность клиента подтверждена" / "Customer identity verified" comes before account work (`p1-ru-006`, `p1-ru-010`, `p1-kz-003`, `p1-en-006`).
- **Borderline:** `p1-ru-002`: the operator said identification was needed, the client declined, nothing was revealed. Options: **N/A** (no protected action happened; recommended) or Да (the operator correctly enforced it).

### 03 vs 08: counting the same mistake twice *(highest-stakes decision)*
- Revealing an account restriction without verification: is it also 08 "раскрытие конфиденциальной информации"?
  - **Option 1 (recommended):** 08 = Нет; the mistake is captured by 03. The dataset looks like one trap per chat, and the client is presumably the account owner.
  - **Option 2:** 08 = Да, reading "confidential information" literally.
- Ask the organizers if you can. Otherwise pick one and write it into `global_rules`.

### 04 Consultation: scale steps
- `p1-ru-003`: client asked about the **commission**, operator explained where the **tariff name** is → 0 or 5 (doesn't answer the question).
- `p1-kz-014`: "Can I get 1042-S in Kazakh?" → "Иә (yes), it's provided in English" → the content is correct but the "yes" contradicts it: 15 for 04, and 06 may get 7.
- `p1-ru-012`: the plan.md doubt is unfounded; the operator answered the asked question directly → 20.
- Follow-up question ignored (`p1-ru-007`, `p1-en-007`, `p1-en-014`): main issue only partly solved → 10 or 15.
- `p1-ru-025`: client left before stating a request → **N/A**. A good redistribution test (01, 05, 06 apply; total 100).

### 06 Conduct
- The first-response delay never counts (checklist: the 3-minute threshold is excluded).
- `p1-ru-028`: "Сейчас проверю путь" and then a 2-minute gap → the operator warned about the wait → 10.
- `p1-ru-005` (vague path, client asks again) and `p1-ru-016` ("Ну, тогда сайт.") → 4.
- Calls: speech-to-text garble must **not** lower the grammar part of 06.

### 07 Closing *(biggest trap)*
- **Literal reading:** "summarized OR offered more help, AND said goodbye". About 35 of 60 DEV chats end with only thanks + goodbye ("Пожалуйста. Хорошего дня!"), which under this reading would all be Нет. That would make most chats multi-trap, which doesn't fit the clearly designed one-trap-per-chat dataset.
- **Likely intended traps:** `p1-ru-014` "Хорошего дня." (curt), `p1-en-015` "Bye.", `p1-kz-005` "Жақсы." (no goodbye), `p1-kz-008` (no closing at all), `p1-en-014` "Goodbye." with the client's question still open, `p1-ru-029` "До свидания." after the insult.
- **Proposed rule:** Да if the client confirmed the issue was solved (or the next step was given) and the operator said a polite goodbye. Нет if a question was left open, or the operator closed without a goodbye.
- **"Operator closed the chat" without a goodbye** (`p1-ru-018`, `p1-ru-022`, `p1-ru-026`, `p1-kz-013`, `p1-en-013`) → Нет, as in the checklist's own example.
- **Connection lost 18–28 s after the client said they were done** (`p1-ru-012`, `p1-en-009`, `p1-kz-011`) → **N/A** (the chat broke off before the operator could close).
- **Calls:** the "Клиент завершил разговор" system event is sometimes timestamped **before** the operator's last line (`p4-ru-002`, `p4-ru-005`, `p4-ru-004`), so don't assume a clean order.
- *Count is approximate (by eye). Confirm by labeling.*

### 08 Gross errors
- **Да:** an offer to bypass the company, even when the client refuses (`p1-ru-022` "Скиньте деньги мне на карту", `p1-ru-023`, `p1-kz-013`, `p1-en-013`); an insult (`p1-ru-029` "Вы вообще читать умеете?").
- Don't let the model be lenient because the client refused. The checklist says the offer alone is enough.

---

## 5. Risks to Ground-Truth accuracy (ranked)

1. **07 literalness** (§4). Could cost a point on most dialogues.
2. **Нет vs N/A** on 02, 03, 04, 07. Models lean toward "Нет". Mitigation: the applicability check comes first in every call; examples in `global_rules`.
3. **Penalizing one mistake under both 03 and 08.**
4. **Picking a neighboring scale step on 04/06.** Mitigation: put each step's text into the prompt verbatim; let the 3-run vote pick.
5. **Calls:** noisy speech-to-text, bot/IVR lines in the operator's position, out-of-order events, kk→ru language-switch system lines. Mitigation: a call-specific note in `global_rules` (data, not code).
6. **Leniency**: LLMs tend to grade generously. Check how often each answer appears against your labels.
7. **Unknown metric**: per criterion, total score, or flag? Optimize per-criterion exact match; ask the organizers.
8. **Overfitting to DEV**: no per-ID rules; any examples must be synthetic or come from appeals.
9. **Small control set (10 dialogues)**: one flip on 07 is a 10% swing on that criterion. Stability (voting) matters as much as intelligence.

---

## 6. Experiments to run (to decide with data, not literature)

Label 20 DEV chats by two people independently, starting with the chats named in §4. Then compare on per-criterion agreement:

| Experiment | A | B |
|---|---|---|
| Splitting the calls | one call for all | one call per criterion |
| Reasoning position | answer first | reasoning first |
| Stability | 1 run | 3 runs with voting |
| 07 rule | literal | proposed rule in `global_rules` |

---

## 7. Dynamics 365 embedding constraints (Microsoft Learn)

- HTML web resources are static files stored in Dataverse and shown in an **iframe** on forms, in the site map, or in ribbons. No server-side code runs.
- **Xrm access:** `parent.Xrm` is deprecated. The supported route is `formContext.getControl("x").getContentWindow().then(w => w.init(data))`, or a single query-string parameter named `data` (pack several values into it).
- **Paths:** relative only (`../styles/app.css`), names like `new_/cqa/index.html`; never root at `/WebResources/...`.
- **File types:** html, css, js, xml, png, jpg, gif, ico, svg, xsl, resx. **No .json** → embed config/data in .js. Max size is `MaxUploadFileSize`, 5 MB by default.
- **CSP:** the default allows inline scripts; the optional strict CSP may block inline scripts and CDNs → keep files self-contained, no CDN.
- **CORS** for external APIs from a web resource: not covered in the docs (unverified). The API must allow the Dataverse origin. Microsoft says PCF / custom pages are the modern alternative.
- **Design implication:** one `api.js` adapter + one `init(data)` entry; each screen (list, card, editor, appeals) as its own module/page; a form may re-create the iframe, so initialization must be safe to run twice.

---

## Sources

**LLM-as-judge / rubric prompts** (mostly search-summary level)
- CheckEval — https://arxiv.org/abs/2403.18771
- G-Eval — https://arxiv.org/abs/2303.16634
- RocketEval (ICLR 2025) — https://mlanthology.org/iclr/2025/wei2025iclr-rocketeval
- HealthBench — https://arxiv.org/html/2505.08775v1 , https://openai.com/index/healthbench/
- RUVERBENCH (bundled rubrics hurt accuracy; preprint, unverified) — https://arxiv.org/pdf/2606.29920
- Rubric interference (isolation setting) — https://arxiv.org/pdf/2608.14684
- Ingle et al., Observe.AI, EMNLP 2024 Industry (contact-center QA with LMs) — https://aclanthology.org/2024.emnlp-industry.60
- Checklist-based evaluation overview (TICK) — https://www.emergentmind.com/topics/checklist-based-evaluation-method
- Practitioner guides: https://arize.com/docs/ax/concepts/evaluators/evaluator-best-practices , https://www.openlayer.com/blog/llm-as-judge-evaluation-guide.md

**Dynamics 365**
- https://learn.microsoft.com/en-us/power-apps/developer/model-driven-apps/web-resources
- https://learn.microsoft.com/en-us/power-apps/developer/model-driven-apps/webpage-html-web-resources
- https://learn.microsoft.com/en-us/power-apps/developer/model-driven-apps/clientapi/reference/controls/getcontentwindow
- https://learn.microsoft.com/en-us/power-apps/developer/model-driven-apps/sample-pass-multiple-values-web-resource-through-data-parameter
- https://learn.microsoft.com/en-us/power-platform/admin/content-security-policy

**QA dispute / calibration UX**
- Zendesk QA disputes — https://support.zendesk.com/hc/en-us/articles/7043760048922-Disputing-a-review
- Zendesk QA calibration — https://support.zendesk.com/hc/en-us/articles/7897784842522
- Voxjar disputes — https://help.voxjar.com/en/articles/11980054-how-to-configure-and-manage-agent-disputes
- MaestroQA appeals — https://help.maestroqa.com/en/articles/2794849-create-an-appeal , https://help.maestroqa.com/en/articles/7833067-agent-qa-appeal-decision-reasons
- Kaizo, disputing AI QA scores — https://kaizo.com/blog/how-to-dispute-an-ai-qa-score/
