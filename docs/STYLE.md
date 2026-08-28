# Writing style

House rules for prose in this repo: README files, docs, code comments,
docstrings, error messages, log lines, and commit messages. They do not apply
to code, identifiers, command syntax, or any name the reader must match against
a screen (button labels, field names, status values, error codes).

The rules condense two sources: ASD-STE100 Simplified Technical English and
Orwell's six rules from "Politics and the English Language" (1946).

The complete rule files live in `docs/writing/` (`ste.md`, with `orwell.md` as
the short version). AI coding tools pick them up through `AGENTS.md` at the
repo root. If you use one to write, test, or update files here, keep it
pointed at those rules.

## Core rules

- Use active voice. Write "the parser reads the file", not "the file is read
  by the parser". Use passive only when the actor is unknown.
- One instruction per sentence. Keep instructions under 20 words and
  descriptions under 25.
- Use one name for one thing. Do not call the same item a "job" in one place
  and a "task" in another.
- Use the short common word: use, not utilize; start, not initiate; before,
  not prior to; to, not in order to; about, not approximately.
- Write numbers as numerals: 3 seconds, not a few seconds.
- Put a condition before its command: "if the file is missing, create it".

## Punctuation

- No em dashes or en dashes in prose. Use a period, a comma, or parentheses.
- No semicolons. Write two sentences.
- No exclamation marks. No emoji in docs, comments, or error messages.
- Plain ASCII quotes and apostrophes only.

## Delete on sight

Hype words carry no facts. Delete them or replace them with a measurement:
seamless, robust, powerful, cutting-edge, battle-tested, comprehensive,
intuitive, enterprise-grade, streamline, leverage, unlock. "Fast" is not a
claim. "Returns in under 40 ms" is a claim.

Filler adds length and nothing else. Delete the whole phrase: it is important
to note that, please note that, as mentioned above, needless to say,
essentially, basically, simply, very, in conclusion.

## Shapes to avoid

- The negation flip: "It is not just X. It is Y."
- The setup question: "So what does this do? Let us look."
- The audience sweep: "Whether you are a beginner or an expert..."
- The rule of three as decoration: "fast, simple, and reliable."
- A closing summary that repeats what the reader just read.

## Per artifact

- **README:** first sentence says what the thing is. Second says what problem
  it solves. Show a working command before the options.
- **Error messages:** state what failed, the value that caused it, and the
  fix. "The upload failed. The file is 82 MB and the limit is 25 MB." Never
  "an error occurred".
- **Commit messages:** describe what the change does and why. No decoration.
- **Comments:** state the constraint the code cannot show. Do not narrate the
  next line.
