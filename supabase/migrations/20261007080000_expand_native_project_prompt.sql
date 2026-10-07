-- New Projects receive native Git instructions. Existing unmodified defaults
-- are converted only by the checked recovery artifact; custom text is retained.
BEGIN;
ALTER TABLE public.projects ALTER COLUMN prompt_template SET DEFAULT $prompt$This PuppyOne repository uses standard Git. The remote URL is a locator; authenticate with your separately issued Git credential.

Use git clone to obtain the repository. Make local changes, run appropriate checks, then use git add, git commit and git push to publish. Use git fetch and reconcile concurrent changes before pushing again. Permissions and repository policy are checked by the server; a local checkout or remote URL does not grant additional access.$prompt$;
COMMIT;
