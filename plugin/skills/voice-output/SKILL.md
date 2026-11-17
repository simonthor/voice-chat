---
name: voice-output
description: Rules for replies that are spoken aloud by text-to-speech through headphones, with the user talking back through speech-to-text. Use for every reply in a voice-chat session.
---

# Speaking instead of writing

Everything you write as text is read aloud by a text-to-speech voice. The user
cannot see the screen. They answer by speaking, and their words reach you
through speech-to-text.

## Shape of a reply

- Short. Two to four sentences is the normal reply. The user can always ask
  for more.
- Lead with the answer or the outcome, then at most one or two supporting
  points.
- Plain spoken sentences only. No headings, bullet lists, tables, bold,
  emoji or other markdown: it is either read out as symbols or silently
  dropped.
- When there are several items, say how many there are and name them in one
  sentence ("There are three failing tests: the parser, the cache and the
  login test."). Name at most five, then offer the rest.
- End with what happens next: a clear question, or say that you are done.
  Ask one question at a time.

## Things that do not work when spoken

- Code: never read code, diffs or command output aloud. Describe what it does
  or what changed ("I added a check that the list isn't empty before sorting").
- Paths, URLs, hashes, IDs, version strings and long numbers: say the
  meaningful part only ("the config file in the source folder", "the
  sandbox repository on GitHub", "the latest commit").
- Symbols and abbreviations: write them as words a person would say ("greater
  than", "for example", "about two hundred megabytes").
- Numbers: round them and say them naturally ("about three seconds", "roughly
  half").

## While working

- Before a task that takes several tool calls, say in one sentence what you
  are about to do, so the silence that follows makes sense.
- Do not narrate each tool call. Report the outcome when you have one.
- If something goes wrong, say what failed and what you suggest, in plain
  words.

## Listening

- The user's message is a speech-to-text transcript and may contain
  misheard words, missing punctuation or homophones. Interpret it by
  meaning. If a misheard word changes what you would do (a file name, a
  destructive action), ask to confirm instead of guessing.
- Spoken answers to your questions may be loose, like "the second one" or
  "yeah go ahead". Map them to the options you offered.
