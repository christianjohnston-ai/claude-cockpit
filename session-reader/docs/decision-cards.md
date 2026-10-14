# Decision cards

When a reply from Claude contains a decision block in the shape below, the reader draws it as a
card: the question in large type, one button per option with its consequence underneath, the
recommended option marked, the reasoning beneath, and the context folded away. In the newest reply,
the buttons are live: click an option to arm it, click again within 4 seconds to send it, or type
your own answer in the box. The answer is typed into that session's prompt and Enter is pressed.

A reply that does not match the shape is shown as ordinary text. Nothing is guessed.

## The shape

A decision block is recognised in either of two ways.

**Fenced.** A code block (three backticks, with or without a language name after them) that
contains the words `DECISION <n> of <m>` anywhere inside it, where `<n>` and `<m>` are numbers.
Rule lines inside a fenced block are optional and ignored.

**Bare.** Outside any code block, a rule line directly followed by a line containing
`DECISION <n> of <m>`. A rule line is a line made only of 8 or more box-drawing horizontal lines
(`─` U+2500 or `━` U+2501), with optional spaces around them. The block runs from that first rule
line to the next rule line after the header. One more rule line straight after the header is
allowed (it closes the header). There must be no other rule line inside the block, or the block
ends early. If no closing rule line follows, the text stays ordinary text.

Inside the block, each line is read with its surrounding spaces trimmed:

1. **Header.** A line starting with `DECISION <n> of <m>`, then an optional separator (a colon
   or a dash), then a short label. The card shows "Decision n of m" and the label.
2. **QUESTION** on a line by itself. The lines after it, up to the next section word, are joined
   into the question.
3. **OPTIONS** on a line by itself. Each option starts on its own line with a capital letter and
   a closing parenthesis, then its label: `A) <label>`. A following line that starts with `→`
   (U+2192) is the consequence of that option; any lines after it are added to the consequence.
   Lines between the option line and its arrow line are added to the label.
4. **RECOMMENDED → X** on one line, where `→` is U+2192 and `X` is the capital letter of one of
   the options. Anything after the letter on that line is ignored. The lines after it are the
   reasoning, shown as "Why X:".
5. **CONTEXT** on a line by itself. Every line after it, to the end of the block, is kept with
   its indentation and drawn as markdown (so bullet lists stay lists), folded under "Context".

Section words must be capitals and alone on their line (except RECOMMENDED, which carries its
letter). Blank lines are ignored, except inside CONTEXT. Lines made only of 6 or more of
`─ ━ = _ -` are treated as separators and skipped. Several decisions in one reply each become
their own card; number them `1 of 3`, `2 of 3`, and so on.

## What gets sent

Clicking an option sends one line: `<n> → <letter>`, for example `2 → B`. The option label is
never typed, because it is text from a reply and a reply is not trusted. A typed answer is sent
as typed (one line, up to 300 characters) but may not start with `!`, `/`, `&`, `#`, `@`, `$`, `~`
or `-`, may not be only a number, and may not contain a backslash or any invisible character.
A card is clickable only while Claude is waiting at its prompt; while it is working, or showing
you a question, it is disabled. Only the newest reply's cards are live, and only while nothing
has been typed after that reply. Older cards show which letter you chose, read from the next
message you sent: a bare letter (when there is one decision), or the decision's number followed
by the letter, as in `2 → B`, `2) B`, `2: B` or `2 B`.

## The skeleton

Placeholders are in angle brackets. Fence the block in a code block, or keep the rule lines and
leave it bare.

```
────────────────────────────────────────
 DECISION <n> of <total>: <short label>
────────────────────────────────────────

QUESTION
  <the question, in one sentence>

OPTIONS
  A) <option label>
     → <what happens if this is chosen>
  B) <option label>
     → <what happens if this is chosen>

RECOMMENDED → <letter>
  <why this option, and what it gives up>

CONTEXT
  <everything needed to answer without a follow-up question>
────────────────────────────────────────
```

## Snippet for your CLAUDE.md

Paste this into `~/.claude/CLAUDE.md` (all projects) or a project's `CLAUDE.md`, so Claude writes
its questions in the shape the reader turns into cards:

````markdown
## Asking me to decide

When you need me to choose between options, write each choice as its own decision block inside
a code block, in exactly this shape, one block per question:

```
────────────────────────────────────────
 DECISION <n> of <total>: <short label>
────────────────────────────────────────

QUESTION
  <the question, in one sentence>

OPTIONS
  A) <option label>
     → <what happens if this is chosen>
  B) <option label>
     → <what happens if this is chosen>

RECOMMENDED → <letter>
  <why this option, and what it gives up>

CONTEXT
  <everything I need to answer without asking you a follow-up>
────────────────────────────────────────
```

Use capital letters for options, the → arrow before each consequence and after RECOMMENDED,
and always recommend one option. End your reply with the question.
````

## Also: brief blocks

A code block that contains a line starting with `GOAL:` and also contains `SUCCESS TEST:` is drawn
as a small summary panel instead of code (this check runs before the decision check). Its rows are the lines starting with `GOAL:`,
`PAYLOAD:`, `CONSTRAINT:` and `SUCCESS TEST:`; a line that starts with none of these continues the
row above it. This is optional and has no buttons.
