# Ship as a standalone `uvx` CLI, not a ruff or flake8 plugin

The original goal was "a new ruff rule." Ruff has no third-party plugin API — rules live in ruff's Rust crate and must be merged upstream; popular flake8 plugins are reimplemented in Rust by the ruff team rather than loaded. Ruff also cannot load flake8 plugins. A real ruff rule would therefore mean forking ruff and writing Rust, which is hostile to the fast break/fix agent loop at the heart of this project (cargo builds + the LLM editing Rust per iteration).

We decided to ship `evloop-lint` as a standalone Python CLI, runnable as a one-liner via `uvx evloop-lint <path>`. The valuable IP is the detection algorithm, which is language-agnostic; a future port into ruff (Rust) remains possible as a downstream transcription once the algorithm stabilizes, but is explicitly out of scope for now.

## Consequences

- The adversarial loop iterates on pure Python: seconds per cycle, high reliability.
- We own AST traversal, output format, exit codes, and `# noqa`-style suppression — we can emulate ruff's UX without depending on ruff.
- Ruff's built-in `ASYNC2xx` rules already catch *flat* blocking calls; evloop-lint's differentiator must be the *nested / indirect / reachable-from-async* cases ruff cannot follow.
