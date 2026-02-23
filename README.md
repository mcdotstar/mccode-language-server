# mclsp — McCode DSL Language Server

A [Language Server Protocol](https://microsoft.github.io/language-server-protocol/)
implementation for the McCode domain-specific languages: **McStas** (`.instr`, `.comp`)
and **McXtrace** (`.instr`, `.comp`).

Built on top of [`mccode-antlr`](https://github.com/mccode-dev/mccode-antlr) and
[`pygls`](https://github.com/openlabs/pygls).

## Features

| Feature | Status |
|---|---|
| Syntax error diagnostics | ✅ |
| Keyword completion | ✅ |
| Component name completion | ✅ |
| Parameter completion inside `Component(…)` | ✅ |
| Hover: component signature & description | ✅ |
| Syntax highlighting (TextMate grammar) | ✅ |
| Go-to-definition | 🔜 |

## Installation

```bash
pip install mclsp
# or
conda install -c conda-forge mclsp
```

Requires `mccode-antlr >= 0.18.0` and a McCode installation (McStas or McXtrace)
for component library lookup.

## Editor setup

### VS Code

Install the **McCode** extension from the Marketplace (it bundles `mclsp`), or
point the generic *LSP client* extension at `mclsp --stdio`.

A minimal extension stub is provided in [`vscode-extension/`](vscode-extension/).

### Neovim (nvim-lspconfig)

```lua
require('lspconfig').mclsp.setup {}
```

A config will be submitted upstream to `nvim-lspconfig` once the server stabilises.

### Helix

Add to `~/.config/helix/languages.toml`:

```toml
[[language]]
name = "mccode"
scope = "source.mccode"
file-types = ["instr", "comp"]
language-servers = ["mclsp"]

[language-server.mclsp]
command = "mclsp"
args = ["--stdio"]
```

## Running the server manually

```bash
mclsp --stdio          # communicate over stdin/stdout (standard LSP mode)
mclsp --tcp 2087       # listen on TCP port 2087 (useful for debugging)
```

## Development

```bash
git clone https://github.com/mcdotstar/mccode-language-server.git mclsp
cd mclsp
uv sync
uv run pytest
```
