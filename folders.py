#!/usr/bin/env python3
"""The folder taxonomy — one definition, used everywhere.

Every note gets exactly ONE folder, so it has a single home on disk and a
single value in Notion. Cross-cutting facts (that something is a book
recommendation, a quote, a tool) stay in `tags` and `content_type`, which are
free to overlap.

Kept in its own module because the rules are consumed in three places that
must agree: the prompt sent to the model, the JSON schema that constrains its
answer, and the validator that catches anything invalid.
"""

import re

CONTENT_IDEAS = "Content Ideas"
MOTIVATION = "Motivation"
STARTUP = "Startup"
TOOLS_AI = "Tools & AI"
LEARNING = "Learning & Self"

FOLDERS = [CONTENT_IDEAS, MOTIVATION, STARTUP, TOOLS_AI, LEARNING]
DEFAULT = LEARNING  # broadest bucket; used when the model returns nonsense

DESCRIPTIONS = {
    CONTENT_IDEAS: "making content: hooks, formats, reel/post ideas, scripts, "
                   "storytelling, filming, editing, captions, posting strategy, "
                   "growth and distribution",
    MOTIVATION: "quotes, mindset, discipline, resilience, faith, philosophy, "
                "identity and purpose",
    STARTUP: "building a business: investors, accelerators, fundraising, pitching, "
             "business models, monetization, sales, hiring, founder operations",
    TOOLS_AI: "a specific tool, app, AI model, automation or prompt workflow",
    LEARNING: "how to think, learn and live: journaling, mental models, note-taking, "
              "reading, health, habits, relationships, career decisions, education",
}

# The tie-breaks are the whole point. Without them the model files anything
# founder-adjacent under Startup and anything with a tool in it under Tools & AI.
RULES = f"""\
## Folder (exactly one — this is where the note is filed)

Pick the ONE folder matching what the viewer would USE this for:

- **{CONTENT_IDEAS}** — {DESCRIPTIONS[CONTENT_IDEAS]}
- **{MOTIVATION}** — {DESCRIPTIONS[MOTIVATION]}
- **{STARTUP}** — {DESCRIPTIONS[STARTUP]}
- **{TOOLS_AI}** — {DESCRIPTIONS[TOOLS_AI]}
- **{LEARNING}** — {DESCRIPTIONS[LEARNING]}

Tie-breaks, in order:

1. **Purpose beats subject.** "10 things founders should post" is about posting →
   {CONTENT_IDEAS}, not {STARTUP}. "How to pitch investors" is about the business →
   {STARTUP}.
2. **Tool vs outcome.** If the point is *which tool and how to drive it* → {TOOLS_AI}.
   If the tool is just the means to an outcome ("build a PR pipeline with AI") →
   file by the outcome ({CONTENT_IDEAS} here).
3. **Recommendations are NOT a folder.** A book, podcast or channel is filed by its
   SUBJECT — a book about discipline → {MOTIVATION}; a podcast about fundraising →
   {STARTUP}. Record that it's a recommendation in `content_type` and `tags`.
4. **Quotes** go to {MOTIVATION} unless the quote is squarely about one of the other
   folders (a quote about fundraising → {STARTUP}).
5. **Inspiration vs instruction.** Something that makes you *feel* like acting →
   {MOTIVATION}. Something that tells you *how* → the matching practical folder.
6. If two still fit, choose the one you'd look in first to act on it. Never invent
   a folder name and never leave it blank — worst case use **{LEARNING}**.
"""


# Whole-word matching, deliberately: a substring check put "fundraising" in
# Tools & AI because it contains "ai".
_HINTS = [
    (STARTUP, r"business|founder|invest\w*|startup|fundrais\w*|accelerator|pitch|revenue|vc"),
    (TOOLS_AI, r"ai|a\.i\.|tool\w*|app|apps|software|automation|prompt\w*|llm"),
    (CONTENT_IDEAS, r"content|reel\w*|hook\w*|post\w*|video\w*|script\w*|caption\w*|creator"),
    (MOTIVATION, r"quote\w*|mindset|motivat\w*|discipline|inspiration|philosophy"),
    (LEARNING, r"learn\w*|self|habit\w*|journal\w*|health|study|education"),
]


def normalize(value) -> str:
    """Map whatever the model returned onto a real folder."""
    v = str(value or "").strip()
    if not v:
        return DEFAULT
    low = v.lower()
    for f in FOLDERS:
        if low == f.lower():
            return f
    # Tolerate near-misses ("Startup Tips", "AI Tools", "content") rather than
    # dumping an otherwise-good note into the default. Startup is checked first
    # so "fundraising" can't be captured by a weaker pattern.
    for folder, pattern in _HINTS:
        if re.search(rf"\b(?:{pattern})\b", low):
            return folder
    return DEFAULT


def safe_dirname(folder: str) -> str:
    """Folder name as a filesystem directory.

    Deliberately identical to the Notion value — an earlier version rewrote
    "&" to "and", so the same folder was called "Tools and AI" on disk and
    "Tools & AI" in Notion, which just looks like two different folders.
    Only path separators are unsafe in a directory name.
    """
    return normalize(folder).replace("/", "-")
