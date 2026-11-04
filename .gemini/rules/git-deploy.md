---
description: Always automatically commit and push to GitHub
globs: "*"
---

# Auto Git Deploy Rule

Whenever changes or fixes are made to the codebase:
1. Verify syntax and logic.
2. Automatically stage, commit with a descriptive message, and push directly to GitHub (`git push origin main`).
3. Verify that GitHub remote receives the push successfully.
4. Do not ask or wait for the user to manually run git commands or deploy.

