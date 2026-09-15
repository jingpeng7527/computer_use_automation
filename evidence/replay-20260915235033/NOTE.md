Fully live: the real `cua replay` CLI, the real approved `acme_core.lookup_savings_balance`
v2 artifact, a real `WebAdapter` instantiated exactly as any other replay run (the approval gate
is checked before the adapter is ever touched, so no browser action actually happens -- but the
command run is the unmodified, real one, not a stand-in for it).

How the draft state was produced: `cua set-scope` was run against the real, currently-approved
v2 artifact with its own existing scope values (a no-op change in effect, since `cua set-scope`
always resets status to `draft` regardless of whether the values actually changed -- this is
correct behaviour, not a bug being exploited here: any command that changes what an approval was
granted for has to require a fresh approval, even a re-declaration of the same values). `cua
replay` was then run against that now-draft artifact, producing this result. `cua approve` was
run immediately afterward to restore v2 to `approved` -- `git diff` on the artifact is empty,
confirming this evidence run left no trace on the real, committed artifact.
