# OpenWhisper · Rune desktop

A native Rust interface built with [Anomalyco Rune](https://github.com/anomalyco/rune).
See [setup, controls, architecture, and validation](../docs/rune-desktop.md).

From a configured OpenWhisper Python environment:

```sh
python main.py --rune
```

For an interactive preview with synthetic data and no Python service:

```sh
cargo run --locked --manifest-path native/Cargo.toml -- --demo
```
