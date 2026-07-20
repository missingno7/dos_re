# tiny_frame_game — dos_re 3.0 in one run

This synthetic DOS game is small enough to read in one sitting: a retrace-wait
frame loop, keyboard ISR, and framebuffer row painted from two state bytes.

```bash
python examples/tiny_frame_game/walkthrough.py
```

The walkthrough demonstrates the complete authority chain:

| Stage | What it proves |
|---|---|
| stable identity | the program image and recovered draw function have shared backend-independent identities |
| Recovery IR | retained static structure is imported without Atlas decoding the EXE again |
| ReplayArtifact | one base continuation and immutable events replay deterministically; function visits are lightweight timeline evidence |
| Execution Atlas | IR and replay evidence produce conservative ProgramCoverage |
| planning | one ImplementationCatalog and identity set produce development and package-ready release plans |
| detachment | DetachmentReport proves the release selection does not require the original EXE |
| continuation | a captured machine continuation restores deterministically |
| verification | a one-byte-short faithful replacement is caught; the correct replacement passes call and frame comparison |
| state view | named authoritative fields project the same DOS-memory bytes verified by the oracle |

The development and release plans are not different games. They select from the
same implementation catalog under different policies. The example’s release
plan uses a native bootstrap declaration and complete coverage; a real port
would also provide the explicit file closure consumed by closed-world export.

The low-level CPU hook in the verifier stage is intentionally shown as backend
machinery. The authored draw function itself is natural Python; its adapter
handles registers, memory, return control, and hook installation.

See [Getting started](../../docs/getting_started.md) for the real port workflow.
