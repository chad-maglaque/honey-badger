# 🧮 Problem solver

**Who can use it:** everyone on the Badger's Wi-Fi, like *Ask the Library* (the admin can limit it to family members). The admin chooses and tests the AI models.

The problem solver works out **real problems**, step by step, and shows its working:

- **Power:** solar panels and batteries, wire sizes and voltage drop, inverters, how long a battery lasts.
- **Water:** how much flows down a pipe from a spring, pump size, bleach dosing, boiling at altitude.
- **Building and rigging:** beams and shelves, rope and strap loads, levers and pulleys.
- **Heat and fuel:** heat lost through walls, how much wood or propane you need, how long water takes to boil.
- **Radio:** antenna lengths, how far a radio reaches, line of sight.
- **Maths, games and decisions:** exact arithmetic, unit conversion, payoff tables (who should do what), expected value, decision trees, sharing out scarce resources (linear programming).
- **Simple code:** short Python programs, tested before you see them (when the Badger's sandbox is installed).
- **Survival planning:** water and food needs, rationing, walking times, distances and bearings from coordinates, weather signs.

It is **not** a writing assistant: ask it to write a letter or an essay and it will politely say so, and offer to help with the practical problem behind it.

## How to use it

1. Open **Problem solver** in *More* on the home screen.
2. Describe the problem with the numbers and units you know ("a 100 W panel, 4 hours of winter sun, a 12 V 100 Ah battery…"). Units can be mixed: feet and metres, gallons and litres, °F and °C.
3. Tap **Solve**. On this small computer a hard problem takes **a few minutes** (sometimes 10–20 with the big reasoning model). You can watch each step: the plan, every calculator it uses and what came back.
4. You can leave the page, lock your phone or reload: the problem keeps going, and the page finds it again (also under *Your recent problems*). **Cancel** stops it.
5. The answer comes with **Working**, **Assumptions**, **Checks** (it re-checks units and sizes, and works the key number out a second way where it can), **Sources** from the library, and a **Confidence** level. If it can't verify something it says so instead of guessing.

The **Calculators** box at the bottom of the page uses the same calculators directly, with no AI: pick one, fill in the numbers, done in a second.

Only one problem is worked on at a time; others wait in line (the page shows your place). Each phone can have one problem going at once.

## Medical questions

The solver looks medical questions up in the library first and gives **only what the articles say**, with a clear warning. Any dose in its answer that isn't written in those articles is **removed** and flagged. It never makes up doses. Always get trained help if you can.

## Family members

If you're a family member, the solver may read your household's **Supplies** list (read-only) when it helps, for example "how long will our water last at half rations?". Visitors' problems can never see it, and a family member's problems (and answers) are visible only to that family member.

## Safety

- The AI can only use **calculators**, the **library** and (if installed) a locked-down **Python sandbox**. It cannot send messages, post on the boards or the radio, change settings, or read your files.
- Library articles are treated as information, never as instructions, and are cleaned before the AI sees them.
- The Python sandbox runs each program as a throw-away user with **no network** and **no access** to family data, keys or settings, for at most 10 seconds and 256 MB. The Badger checks that this is true before every use; if it can't prove it, the Python tool stays switched off (the solver still works without it).

## AI models (admin)

The solver uses AI models by **role**:

| Role | Default model | Size | Speed on a Pi 5 (estimate) | Used for |
|---|---|---|---|---|
| Fast | Qwen3 4B Instruct | 2.5 GB | 3–5 words/s | *Ask the Library* |
| Reasoning | Qwen3 8B (thinking) | ~5 GB | 1.5–2.5 words/s | deep solves |
| Reasoning (alternative) | Qwen3 4B Thinking 2507 | ~2.5 GB | 3–5 words/s | deep solves, faster |
| Coder | Qwen2.5 Coder 1.5B | ~1.1 GB | 8–10 words/s | programming questions |
| Endurance | Qwen2.5 1.5B | 1.1 GB | ~10 words/s | battery-saving mode |

All are Apache-2.0. Only one model is in memory at a time: for a deep solve the Badger loads the reasoning model (about a minute), and goes back to the fast model after 10 minutes without deep work. It **never loads a model that doesn't fit in free memory**; it says why instead and uses the model already loaded. In battery-saving mode it keeps the small model.

On the solver page, admins see **AI models**: for each role, the models in the catalog, which are downloaded (and their version), the self-test result, and buttons to **Run self-test**, **Make active**, **Load now** and **Roll back**.

**Self-test:** six problems with known answers (exact fractions, a unit conversion, a physics drop, an electrical load, a payoff table, a short code task), run through the full solver. It shows the score and the speed (tokens per second). **A new model only becomes active if it scores at least as well as the current one** (an admin can override this).

**Updating models:** better models arrive as new catalog entries with signed software updates, a USB update bundle or another Badger, and download like any other content (*Updates*). Then run the self-test and promote it. New model files are marked "checksum not verified" until a maintainer has confirmed them.

## Hardware

Nothing extra. The reasoning model needs the **8 GB** Pi 5; a 4 GB Badger uses the 4B models.

## Settings

`[solver]` in `/etc/prepper/prepper.toml` (all optional; see `os/prepper.toml.example`): `enabled`, `access` (`public` or `member`), `per_ip_per_hour` (6), `max_queue` (6), `max_steps` (10), `context_tokens` (0 = ask the AI server), `idle_minutes` (10), `job_ttl_hours` (12), `job_timeout_minutes` (45), `token_timeout` (300), `calc_per_10min` (60), `reserve_mb` (1000), `python` (true), `sandbox_socket`, and `[solver.models]` default model per role. Turn the whole feature off with `[features] solver = false`.

## Limits

- It can be wrong. Small offline models make mistakes; the calculators don't, so read the working and the checks.
- Hard problems are slow on battery power and in battery-saving mode.
- Answers are kept for 12 hours, then deleted.
