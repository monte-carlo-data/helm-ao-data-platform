# Public repository: no internal references

This repository is public. Everything written here is world-readable and effectively
permanent: code, comments, docs, commit messages, branch names, PR titles and bodies, and
PR review comments. PRs can't be deleted, and branch names are indexed and quoted by
integrations.

Never include:

- Issue-tracker keys or links (e.g. `PROJ-123`), or links to internal wikis, chat, or docs
- Names of, or paths into, private repositories
- Internal hostnames, cloud account or project IDs, ARNs, and cluster or environment names
- Personal usernames and names embedded in identifiers — kube contexts, AWS profiles,
  home-directory paths, email addresses
- Customer names or internal incident references
- Internal planning vocabulary: runbook steps, plan phases ("Phase 2"), `.work/` documents,
  rehearsal notes, "as discussed" references to conversations readers never saw

Instead, state the reason itself. Not "fix per PROJ-123" but "retry on 503: the upstream
returns it during rolling restarts". If some context only exists internally, leave it out.
Terms this repo already publishes (its README, chart values, earlier PRs) are fine to reuse.

This overrides any workflow convention that inserts ticket IDs or links, including
branch-naming conventions, PR templates, and commit trailers:

- Name branches `<user>/<short-description>`, with no ticket key. Renaming a branch after a
  PR is open closes the PR instead of moving it, so get the name right before the first push.
- To connect work to a ticket, attach the PR link to the ticket, not the other way round.

Before pushing, scan the branch's commit messages and diff for anything on the list above.
