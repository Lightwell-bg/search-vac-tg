# Prompts

Texts sent to the models, copied from the code. To change them, edit the source files (there is no separate prompt config) and restart the service.

- JEV questions: `QUESTIONS` and `CATEGORIES` in `src/jev/classifier.py`
- OpenRouter prompts: `src/llm/prompts.py`

## JEV (System One, one call per job)

State sent with the questions: `profile` (compact profile), `job` (normalized text, cut to `[jev] max_text_chars`), `keywords_found` (technologies found by the rule filter, up to 15, only if any).

### decision (choice)

Instructions:

```
Should this freelance job post be sent to the developer described in `profile`? Judge by what has to be built, not by single words: automating a design agency with n8n is a fit, a designer job is not.
```

Criteria:

- `accept`: Clearly a technical task the developer can do with the profile stack.
- `reject`: Clearly not a fit: non-technical work (design, SMM, sales, copywriting, video), an ad or not a job at all, or a stack far outside the profile.
- `review`: Ambiguous, mixed or too vague to decide; needs a closer look.

### fit (score 0-3)

Instructions:

```
How well does the job match the developer `profile`?
```

Criteria (index = score):

0. No match
1. Weak match (adjacent technical work, few overlapping skills)
2. Moderate match (doable with the profile stack, partial overlap)
3. Strong match (core of the profile: Python, bots, automation, AI/LLM, integrations)

### category (choice)

Instructions:

```
Main category of the job.
```

Criteria:

- `telegram_automation`: Telegram bots, userbots, channel/chat automation
- `business_automation`: n8n/Make/Zapier workflows, CRM, Google Sheets, API and webhook integrations
- `ai_llm`: AI assistants, LLM/ChatGPT integrations, AI agents, RAG, MCP
- `web_backend`: Python backend, FastAPI, databases, deployment, Docker/VPS
- `wordpress`: WordPress / WooCommerce sites and plugins
- `parsing`: parsing, scraping, data collection
- `other_tech`: other software development outside the profile stack
- `non_tech`: not software development (design, SMM, sales, texts, video, admin work)

### Guard rules (code, not prompt)

In `JevClassifier.parse`: ACCEPT/REJECT with confidence below `[jev] min_confidence` becomes REVIEW; ACCEPT with fit below 1.5 (of 3) becomes REVIEW; REJECT with fit 2.0 or more becomes REVIEW.

## OpenRouter

### Job review (`REVIEW_SYSTEM`, JSON mode)

```
You screen freelance job posts for one developer. Return strict JSON only, no markdown, no explanations outside JSON. Schema: {"fit_score": int 0-100, "category": str, "relevant_skills": [str], "missing_skills": [str], "reason": str (one short sentence in Russian), "should_notify": bool}. should_notify=true only for a real technical job the developer can do with the profile stack. Non-technical jobs (design, SMM, sales, copywriting, video) get fit_score below 30.
```

User message (`review_user`):

```
PROFILE:
{profile}

JOB:
{job}

JEV:
{jev_short}

Return strict JSON only.
```

`{jev_short}` looks like `decision=review (p=0.62), fit=1.80/3, category=web_backend`, or `unavailable (error)` when JEV failed.

### Reply draft (`APPLICATION_SYSTEM`)

```
You write a short personal reply from a freelance developer to a job post, in the language of the post (Russian by default). 500-900 characters, plain text, no markdown headers. Structure: one line showing you understood the task; 2-3 concrete relevant points from the developer's experience and projects (only from the given data, never invent facts, numbers or clients); a short plan or first step; one question to clarify the task. No flattery, no placeholders like [Name].
```

User message (`application_user`):

```
PROFILE:
{profile}

RELEVANT PROJECTS:
- {name}: {summary} [{stack}]
...

JOB:
{job}

Write the reply.
```

Up to 3 projects; if none: `- (no project descriptions available)`.

### Profile polish (`scripts/rebuild_profile.py --llm`)

The prompt of this call lives in `src/profile/builder.py` (`enrich_with_llm`).
