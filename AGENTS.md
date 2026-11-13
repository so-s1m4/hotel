# Working in this repository

Read README.md, docs/DEVELOPMENT.md and common/01_CONTRACT.md before implementation.
This is a scaffold, not a completed challenge solution. Supplied task prompts under
docs/tasks/ are reference assignments; implement the user's requested scope.

- A owns ingestion/ and tests/ingestion/. B owns engine/, run.py and tests/engine/.
- The sole active shared contract is common/. Preserve its public schema/signatures.
  Contract changes require one coordinated change and updated examples/tests.
- Never merge Quickstart and Dev. Only query facts available at replay time.
- Keep source columns, provenance, unknowns and availability timestamps intact.
- No real actuation; simulated results cannot replace observed history.
- Never bypass approvals, guest consent, staff capacity or room-release requirements.
- No keys, .env, datasets, archives, caches or virtualenv in Git.
- Scaffold TODOs must fail explicitly, never silently emit empty success outputs.
- Use python -m pytest -q. Add causal and scenario tests with module implementations.
- Use the supplied example event to develop the engine independently of ingestion;
  never submit illustrative example decisions as observed results.
