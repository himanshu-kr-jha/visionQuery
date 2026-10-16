# The Reasoner reaches memory only through typed tools, and every statement cites Evidence

The Reasoner calls a fixed set of typed tools (resolve an Area, find Candidates, get a Visit, follow, list Tracks, describe a span, list Gaps) whose results are already Evidence.
A statement in an Answer that does not cite returned Evidence is removed or flagged before it is shown, and relevant Gaps are always stated.
The interface calls the same tools, so chat and clicks are two front ends to one set of operations.

## Considered Options

Text-to-SQL was rejected because local models write it unreliably, Evidence cannot be recovered from arbitrary rows, and the schema would become an unchangeable API.
A custom query language was rejected because small models call tools more reliably than they emit a new grammar.
