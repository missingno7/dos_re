# tiny_frame_game — several dos_re capabilities in one run

This synthetic DOS game is small enough to read in one sitting: a retrace-wait
frame loop, keyboard ISR, and framebuffer row painted from two state bytes.

```bash
python examples/tiny_frame_game/walkthrough.py
```

The walkthrough puts several independent capabilities in one convenient
demonstration order:

| Capability | What it demonstrates |
|---|---|
| stable identity | the image and draw function have backend-independent identities |
| Recovery IR | retained static evidence can be imported without Atlas decoding the EXE |
| ReplayArtifact | a base continuation and immutable events replay deterministically; visits add timeline evidence |
| Execution Atlas | static and replay sources can be projected into conservative ProgramCoverage |
| planning | one catalog supports development and package-ready release policies |
| detachment | DetachmentReport explains why the selected release no longer needs the EXE |
| continuation | captured machine state restores deterministically |
| verification | an incorrect faithful replacement is caught and the correct one passes |
| state view | named fields can project the same DOS-memory bytes verified by the oracle |

This is an integration fixture, not a required recovery sequence. An Atlas may
start from replay or explicit facts without IR; a port may use generated code
without state views; another may never need a release export.

The development and release plans select from the same implementation catalog
under different policies. The example uses a native bootstrap declaration and
complete synthetic coverage; a real export also supplies its exact file
closure.

The low-level CPU hook is backend machinery. The authored draw body is ordinary
Python, and its selected adapter handles registers, memory, return control, and
hook installation.

See [Getting started](../../docs/getting_started.md) for ways to combine the
operations in a real port.
