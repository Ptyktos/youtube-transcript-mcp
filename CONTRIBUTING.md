# Contributing

Thanks for considering a contribution. Issues and pull requests are welcome for
bug fixes, URL/language-parsing edge cases, new output formats, and
documentation.

## Before opening a pull request

- Search existing issues and pull requests; describe the user-visible problem
  and expected behavior.
- Keep changes focused. Include or update a regression test when behavior
  changes (`crates/native` has wiremock-backed integration tests against a
  local mock Innertube/caption-XML server).
- Never include real API keys, cookies, or private video/transcript data in
  commits or issue attachments.
- For benchmark claims, follow the methodology in [BENCHMARKS.md](BENCHMARKS.md)
  — regenerate the numbers rather than hand-editing the table.
- For dependency changes, explain why the dependency is needed and keep
  `Cargo.lock` updated. `crates/worker` has its own `[workspace]` so it can
  target `wasm32-unknown-unknown` independently — dependency changes there
  don't touch the host workspace's lockfile.

## Development setup

```sh
cargo build --workspace
cargo test --workspace
cargo fmt --check
cargo clippy --workspace -- -D warnings

# Worker crate (separate workspace, wasm32 target)
cargo check -p youtube-transcript-mcp-worker \
  --manifest-path crates/worker/Cargo.toml \
  --target wasm32-unknown-unknown
```

The workspace lints (`[workspace.lints.clippy]` in the root `Cargo.toml`) deny
`unwrap()`, `panic!()`, and `unimplemented!()` outside test code — write
recoverable errors instead.

Run the native binary locally with `cargo run -p youtube-transcript-mcp`. It
binds `127.0.0.1:3000` by default; `--host`/`--port` change that. Keep local
testing on a trusted machine before changing the bind address.

## Pull requests

Open a pull request against `main`. Include a concise summary, the commands
run and their outcomes, and sample output when it clarifies a change. CI must
pass before merge.

## Security reports

Do not report vulnerabilities in public issues or pull requests. Follow
[SECURITY.md](SECURITY.md) for private reporting.

## License

By contributing, you agree that your contribution is offered under the
repository's MIT license.
