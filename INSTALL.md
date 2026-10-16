# INSTALL

Requirements: Python ≥ 3.9 (stdlib only), git, `muse` CLI, `gh` (optional).

```sh
git clone <this-repo> openmainty && cd openmainty
chmod +x openmainty
ln -s "$PWD/openmainty" ~/.local/bin/openmainty
openmainty doctor            # quick, cost-free
openmainty doctor --live     # one tiny subscription run (spends quota)
```

Windows: run via `python3 src/openmainty/cli.py ...` or
`python -m openmainty` with `src/` on `PYTHONPATH`; path handling is
`pathlib`-based, but Windows is NOT verified (see docs/MULTI_PC.md).

Isolated second setup on the same machine (proves home separation):

```sh
OPENMAINTY_HOME=/tmp/om-b openmainty doctor
OPENMAINTY_HOME=/tmp/om-b openmainty repo list   # empty, independent
```
