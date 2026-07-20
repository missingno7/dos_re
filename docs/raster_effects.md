# Raster-effect evidence

Some programs change indexed-color palette or display-controller state while a
frame is being scanned. A renderer that samples only one palette per frame can
therefore lose observable presentation behavior.

dos_re handles this as optional, composable evidence:

```text
ordered device observations
  -> normalized display operations
  -> evidence-backed semantic projection
  -> selected presentation implementation
```

The DOS device model may retain ordered status reads, palette writes, and
virtual-time positions in its bounded video journal. `dos_re.palette_effects`
normalizes that journal and classifies only effects justified by the observed
ordering and timing. Ambiguous positions remain explicitly unresolved.

This mechanism is not a required recovery stage. A project may:

- compare the raw journal while verifying DOS-memory-backed implementations;
- project verified palette bands for a detached renderer;
- record a cited project fact when hardware observations omit a semantic row;
- omit the mechanism entirely when the program does not use raster effects.

A presentation enhancement consumes the verified projection without becoming
an authority for gameplay state. Its adapter owns display output only, and its
tests compare authoritative continuation state with the enhancement enabled and
disabled.

Project-specific addresses, row mappings, video modes, and renderer choices
belong in project evidence and implementation descriptors. Historical
port-specific design notes are preserved in
[`history/raster_effects_2.0.md`](history/raster_effects_2.0.md).
