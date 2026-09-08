# turbo CLI

Host-side tool for turbo: compiles `@turbo`-decorated CircuitPython modules to
native `.mpy` with the official Adafruit `mpy-cross`, installs them under
`lib/turbo/<arch>/`, and benches candidates on a board.

Private during development. Design and implementation spec: `SPEC.md`.

    pipx install git+ssh://git@github.com/mikeysklar/turbo-cli.git
    turbo doctor
