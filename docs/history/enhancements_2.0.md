# Enhancement lessons from dos_re 2.0 ports

This document preserves port-specific design lessons. It is not a current API
or execution workflow.

Prehistorik 2 experiments established two useful presentation boundaries:

- verify the authoritative state seam before attaching presentation code;
- compare authoritative state with the enhancement enabled and disabled, while
  excluding only the output the enhancement explicitly owns.

Widescreen work showed why “render wider” is not a complete specification.
Objects can be culled or produced relative to the original viewport, foreground
layers and HUD elements can have separate clipping rules, and some scenes have
no meaningful off-screen content. Drawing already-simulated state in additional
space can be presentation-only; changing production, collision, or simulation
to populate that space is authoritative behavior.

The same work separated display shape from game state. A 320 by 200 framebuffer
may be presented with a 4:3 pixel-aspect correction or with square pixels.
Because scaling occurs after the authoritative framebuffer, either choice can
remain a presentation option rather than a gameplay change.

Frame interpolation was safest when it retained two verified presentation
samples and blended only their rendered intent. Feeding interpolated values
back into simulation would have crossed the authoritative boundary.

These cases motivated the 3.0 rule that enhancement read-only behavior is a
declared and tested contract. The framework category is metadata; it is not a
runtime immutability guarantee.
