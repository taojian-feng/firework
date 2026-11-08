<!-- Draft for Taojian to review before sending. Numbers come from results/ (2026-11-08 runs). -->

**To:** Raul Jimenez <raul.j@gitlab.com>
**From:** Taojian, Fireworks AI
**Subject:** Re: Help Needed: Agentic BI CLI Product for GitLab Customers

Hi Raul,

Thanks for the detailed write-up and the sample data. We built a working proof of concept of the
CLI on open models served by Fireworks. Here is what we built, how it performed, and what we
suggest next.

**What we built**

A terminal tool (`uv run cli`) that takes a question in plain English, shows the SQL it wrote,
runs it, and shows the results as a table. Follow-up questions ("now only for female clients")
modify the previous query. The design fixes the three failure modes you described:

- *Hallucinated columns and wrong filters:* the model sees the real schema, your column
  documentation and the actual values in each categorical column (for example the exact region
  spellings and loan status codes). It doesn't have to guess names or values.
- *SQL that doesn't execute:* the model runs its query through a tool. If the query fails or
  returns nothing, the error goes back to the model and it fixes the query before the user sees anything.
- *Trust:* every answer shows the exact SQL plus any assumption the model made (e.g. "good
  standing" read as loan status C), so users can check it. Queries run on a read-only
  connection with a row cap and timeout.

It works against any SQLite file with no setup. Column docs help but are optional.

**Results on the 10 dev questions**

We scored by executing the generated SQL and comparing result sets with your gold answers, so any
correct query counts, however it's written.

| | Your prompt (Kimi K3) | Our agent (Kimi K3) | Our agent (gpt-oss-120b) |
|---|---|---|---|
| Correct results | 5/10 | 10/10 (30/30 over 3 runs) | 10/10 (30/30 over 3 runs) |
| SQL executes | 5/10 | 10/10 | 10/10 |
| End-to-end latency P50 / P95 | 8.9s / 24.0s | 4.8s / 19.3s | **1.6s / 4.0s** |
| Est. cost per day at 30k queries | ~$270 | ~$444 | **~$25** |

Costs use Fireworks serverless list prices, without prompt caching. The five baseline failures were
the problems you described: invented tables and columns (`loans`, `district_name`,
`birth_number`) and non-SQLite syntax. With the schema grounding, none of them happen.

Our recommendation is **gpt-oss-120b**. On this set it matches the larger model's accuracy, beats
your 3-second P50 target (1.6s), and costs about $25 a day for the first cohort, roughly 17x less
than Kimi K3. One thing we'd still harden: in 6 of 30 answers it found the right result with an
exploratory query and then didn't formally submit it. Our agent catches that today, but we'd add
tests for it before you ship. Kimi K3 stays useful as a quality reference. With reasoning turned
down it reaches 2.6s P50 at the same accuracy.

**How we validated it**

- An eval harness (`uv run eval`) that runs every dev question, scores the result sets, and records
  latency, token usage and cost. It runs your original prompt through the same scorer for a fair
  comparison, and we checked that the scorer accepts all 10 gold queries.
- Offline unit tests for the safety guardrails, the agent loop (including self-repair and follow-ups)
  and the scorer.

To be candid, 10 questions is a smoke test, not an accuracy guarantee. The first thing we'd do
together is build a larger eval set from your users' real questions.

**What we'd tackle next (about 2 to 3 weeks)**

1. *Eval set (week 1):* 100 to 200 real questions with verified answers from your team, split by
   difficulty. That becomes the bar for "good enough to ship" and catches regressions.
2. *Your databases:* Postgres/MySQL through a read-only role, and schema retrieval with Fireworks
   embeddings so wide schemas with hundreds of tables still fit in a small prompt.
3. *Latency and cost at scale:* load test at your projected peak, compare 2 to 3 open models
   on the quality-latency-cost curve, and decide between serverless and a dedicated on-demand
   deployment based on measured traffic. The serverless P95 had occasional slow outliers. Dedicated
   capacity gives predictable tail latency, and usually becomes cheaper once volume is steady.

**Post-training (fine-tuning)**

Prompting plus schema grounding gets most of the way. Fine-tuning pays off when the eval set shows
repeated, systematic errors on a stable schema: business definitions the model keeps missing, house
SQL style, or when we want a smaller, faster model to match a large model's accuracy at lower cost.
Our approach:

- *Data:* question → verified SQL pairs. Sources are the eval set, CLI logs where users
  accepted or corrected an answer, and synthetic questions generated from the schema by a larger
  model and filtered by executing them. A few hundred to a few thousand good pairs is usually enough.
- *Method:* supervised fine-tuning (LoRA) of an open model on Fireworks first. Then reinforcement
  fine-tuning with execution match as the reward, which directly optimizes "the results are
  correct" and doesn't need one canonical SQL per question.
- *Gate:* ship only if it beats the prompted baseline on the held-out eval set, and re-evaluate
  whenever the schema changes. Retrieval and prompt changes are cheaper and handle schema drift
  better, so we'd fine-tune for stable patterns and keep schema knowledge in retrieval.

**Next step**

Could we set up 30 minutes next week to walk through a demo? If you can share 50 to 100 real
questions your users would ask (with expected answers if you have them), we'll come back with
measured accuracy, latency and cost on those within a week.

Note: I used Claude Code as a coding assistant while building this. I reviewed all of the code and
can walk through it.

Best,
Taojian
