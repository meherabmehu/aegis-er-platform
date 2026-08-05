# Contributing

Thanks for your interest in AEGIS-ER.

## Development workflow

1. Install dependencies:

   ```bash
   make install
   ```

2. Run the stack locally:

   ```bash
   make run
   # open http://localhost:8000/ for the command center
   ```

3. Run tests before submitting a PR:

   ```bash
   make test
   ```

4. For load testing:

   ```bash
   make load
   ```

## Commit conventions

- Use imperative, present-tense subject lines (`add solver fallback`, not `added`).
- Keep the first line under 72 characters.
- Reference architecture decisions in `docs/adr/` when changing core behavior.
