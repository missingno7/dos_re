# De-SMC incident record from dos_re 2.0

This is historical validation context, not a current workflow or proof for
other programs.

SkyRoads exposed the frozen-lift failure mode in an LZS decoder. The original
routine wrote header-derived bit-width immediates into its own instruction
stream before decoding each compressed file. A generated implementation built
from one menu-time memory image embedded those values and reused them for
later files. The resulting corruption surfaced much later in startup memory
allocation.

The first de-SMC transform re-expressed the supported immediate fields as
reads from live code memory. In the recorded validation, the transformed
decoder processed a complete compressed chunk with 2,148 calls through the
runtime-patched bit reader and matched the interpreted oracle with no byte
difference across the one-megabyte machine image. Startup allocation then
matched the oracle for that workload.

That result justified the supported operand-field transform and the
candidate-then-verify policy. It did not prove arbitrary self-modifying code,
unobserved variants, instruction-shape mutation, or runtime-generated code.
