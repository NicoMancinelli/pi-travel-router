## What does this change do?

<!-- One or two sentences. What problem does this fix or what capability does it add? -->

---

## How was it tested?

- [ ] On-device test on a Pi Zero 2 W
- [ ] Syntax check only (`bash -n`)
- [ ] Full image build (GitHub Actions or local pi-gen)
- [ ] Not applicable (docs / CI only)

<!-- Brief description of what you tested and the result: -->

---

## Checklist

- [ ] `bash -n <script>` passes for every modified shell script
- [ ] `shellcheck <script>` passes for every modified shell script (required if modifying `.sh` files)
- [ ] `CHANGELOG.md` updated under `[Unreleased]` (required for any user-visible change)
- [ ] Commit message follows the `fix:` / `feat:` / `docs:` / `chore:` convention
- [ ] TUI coverage: new `ENABLE_*` flag added to `flag_list` in `show_features()`, new config var to `show_settings()`, new script to TUI menu (see AGENTS.md — TUI Coverage Rule)
