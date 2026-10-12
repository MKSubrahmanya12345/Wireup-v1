# Blueprint hands-on comparison and WireUp iteration

Reference: user-provided `blueprint-research/blueprint-hands-on.md`. It records one actual account/session; untested sharing, physical assembly and CAD fit are not evidence of working hardware.

## Adopted improvements

- Add a durable, discoverable Project details panel with Info, Parts, Wiring and Instructions drawn from the same current project used by the circuit editor.
- Group component quantities by real type and properties, retaining exact references. Avoid pretending placed parts are a complete shopping list; show wire counts as estimates and explicitly call out cable/header/supply review.
- Show exact pin names consistently in the wiring table and deterministic assembly steps.
- Distinguish source presence, compiler result, stale artifact and physical verification. Do not dismiss a wiring warning using an unsupported chat explanation.
- Export current source/project definitions and wiring CSV without private runtime tokens or generated binary blobs. These JSON exports are portable snapshots, not a new validated import/restore API.
- Correct artifact download labels (HEX versus BIN) and align the flash-dialog readiness with source/project revision and successful compiler state.

## Preserved WireUp features

Current AI/tool architecture, project storage, canvas/wiring, compiler, simulator, instruments, MCQs and physical-upload confirmation stay intact. There is no old proposal workflow, no accounts/credits, no replacement circuit model, and no fake generated mechanical geometry.

## Limits and future work

Blueprint's mechanical layout and CAD generation are not implemented here. Its example has missing mounts and unavailable STEP; reproducing the interface alone would not produce validated fabrication outputs. Pricing/budgets require user-provided or verified quotes; no prices are fabricated. Physical step completion is not automatically marked by opening instructions. Source can contain user secrets and must be reviewed before sharing exports.

This iteration supplies project-derived build instructions, not persisted AI mechanical design or a complete accessory/budget manager. Later revisions can add explicit validated accessory/budget records without inserting fake components into simulation.
