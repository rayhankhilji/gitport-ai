# gitport

AI-native pre-merge gatekeeper for production code. `gitport` inspects a git
diff before it lands, pulls in your team's internal engineering rules, runs
real checks (AST analysis, migration hazards, dependency vulnerabilities)
through a Cohere tool-use agent loop, and returns a strict PASSED / WARNING /
FAILED verdict your CI pipeline can gate on.

Powered by the Cohere ClientV2 API: `embed`, `rerank`, and `chat` with
tools + structured JSON outputs.
