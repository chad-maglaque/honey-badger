# Contributing

Thanks for helping make Badgers better.

- **Agreement:** to keep the project's licensing options open (including commercial licences),
  every contributor agrees to the [Contributor License Agreement](CLA.md) once, in their first
  pull request.
- **Licences:** your contributions are released under the licence of the part they touch:
  AGPL-3.0-only (software), CERN-OHL-S-2.0 (case) or CC BY-SA 4.0 (docs). See
  [LICENSING.md](LICENSING.md). Don't add the name or logo to anything new, and don't submit
  code you can't license this way.
- **Code style:** Python services use the standard library only. Shell scripts must pass
  `shellcheck -S warning`. Python must pass `ruff check --select F,B`. The launcher is plain ES5
  JavaScript with no build step, and must work on old iPhones.
- **Tests:** run each test file on its own, e.g. `python3.11 tests/test_members.py`; every file
  must pass. New behaviour needs tests, including bad-input and wrong-access cases.
- **Security:** please report vulnerabilities privately (a GitHub security advisory on this
  repository), not in a public issue. [docs/SECURITY.md](docs/SECURITY.md) describes the
  design.
- **Hardware:** if you change `case/honey_badger_case.scad`, render the assembly and check that
  the layout report prints no `WARNING`.
