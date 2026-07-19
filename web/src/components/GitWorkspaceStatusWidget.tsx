// Collapsible git/PR status bar anchored above the composer.
//
// Contextual: it only appears when the session's workspace actually has
// something to report — uncommitted/committed changes, commits ahead of the
// base, or a pull request for the branch. A clean tree with no PR shows
// nothing, and a git failure hides the bar rather than putting an error in
// front of the chat.

import {
  CheckIcon,
  ChevronRightIcon,
  GitBranchIcon,
  GitPullRequestIcon,
  Loader2Icon,
  XIcon,
} from "lucide-react";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { useGitPrStatus, type GitPrStatus, type GitPullRequest } from "@/hooks/useGitPrStatus";
import { cn } from "@/lib/utils";

/** Mirrors the composer column so the bar lines up with the card below it. */
const COLUMN_WIDTH = "max-w-3xl min-[1921px]:max-w-4xl min-[2561px]:max-w-5xl";

/**
 * Whether the status bar has anything worth showing for `status`.
 *
 * Exported so the composer status line can drop its own branch label while
 * the bar is up, instead of printing the branch twice.
 */
export function isGitWorkspaceStatusVisible(status: GitPrStatus | undefined | null): boolean {
  if (!status) return false;
  // A git failure degrades to silence — the chat is not the place for it.
  if (status.error) return false;
  if (!status.workspace || !status.branch) return false;
  const changed = status.diff
    ? status.diff.added + status.diff.removed + status.diff.files > 0
    : false;
  return changed || (status.ahead ?? 0) > 0 || status.prs.length > 0;
}

/** The first still-open PR for the branch, if any. */
export function openPullRequest(prs: GitPullRequest[]): GitPullRequest | null {
  return prs.find((pr) => pr.state === "open") ?? null;
}

/** Drop the remote prefix of an upstream ref: `"origin/main"` → `"main"`. */
function baseBranchName(baseBranch: string | null): string | null {
  if (!baseBranch) return null;
  const slash = baseBranch.indexOf("/");
  return slash === -1 ? baseBranch : baseBranch.slice(slash + 1);
}

/** GitHub account name: alphanumerics and inner hyphens. */
const OWNER_PATTERN = /^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$/;
/** GitHub repository name: alphanumerics, dot, underscore, hyphen. */
const REPO_PATTERN = /^[A-Za-z0-9._-]+$/;

/**
 * Split `"owner/repo"` into its two encoded path segments.
 *
 * The slug reaches us as free-form text from a git remote, and it lands in
 * the middle of a URL we hand to the browser — a value carrying `?`, `#`, an
 * extra segment or `..` could rewrite the path or point the button at another
 * destination entirely. Only a strict `owner/repo` is accepted; anything else
 * is `null`, which drops the button rather than linking somewhere unintended.
 */
function parseRepoSlug(slug: string | null): string | null {
  if (!slug) return null;
  const parts = slug.split("/");
  if (parts.length !== 2) return null;
  const [owner, repo] = parts;
  if (!OWNER_PATTERN.test(owner) || !REPO_PATTERN.test(repo)) return null;
  // `.` and `..` pass REPO_PATTERN but are path traversal, not repositories.
  if (repo === "." || repo.includes("..")) return null;
  return `${encodeURIComponent(owner)}/${encodeURIComponent(repo)}`;
}

/**
 * GitHub compare URL for opening a PR from `branch` onto the base.
 *
 * Built from the workspace's own remote (`repo_slug`), so it is available on
 * the common case — a dirty branch with no PR yet. Without a usable slug (no
 * remote, one not hosted on github.com, or a slug that isn't a plain
 * `owner/repo`) there is nothing safe to link to and the caller renders no
 * button.
 */
export function compareUrl(status: GitPrStatus): string | null {
  const repo = parseRepoSlug(status.repo_slug);
  const base = baseBranchName(status.base_branch);
  if (!repo || !base || !status.branch) return null;
  return `https://github.com/${repo}/compare/${encodeURIComponent(base)}...${encodeURIComponent(status.branch)}?expand=1`;
}

/** What each settled CI state is called, for both the label and the dot. */
const CI_LABEL: Record<"success" | "failure" | "pending", string> = {
  success: "CI passou",
  failure: "CI falhou",
  pending: "CI rodando",
};

const CI_ICON = { success: CheckIcon, failure: XIcon, pending: Loader2Icon } as const;

/**
 * CI state of a PR: a distinct icon per state (check / cross / spinner) plus
 * its name in text, so the state never rides on color alone. The compact
 * badge keeps the name screen-reader-only — the icon shape already tells the
 * states apart there — while the expanded list spells it out.
 *
 * `null` is "sem informação" (no checks reported, or the server hit its CI
 * lookup budget), which is not a failure and shows nothing.
 */
function CiIndicator({
  status,
  showLabel = false,
}: {
  status: GitPullRequest["ci_status"];
  showLabel?: boolean;
}) {
  if (status === null) return null;
  const Icon = CI_ICON[status];
  return (
    <span
      data-testid={`ci-${status}`}
      className={cn(
        "inline-flex shrink-0 items-center gap-1",
        status === "success" && "text-success",
        status === "failure" && "text-destructive",
      )}
    >
      <Icon className={cn("size-3 shrink-0", status === "pending" && "animate-spin")} />
      <span className={showLabel ? undefined : "sr-only"}>{CI_LABEL[status]}</span>
    </span>
  );
}

const PR_STATE_LABEL: Record<GitPullRequest["state"], string> = {
  open: "aberto",
  merged: "merged",
  closed: "fechado",
};

/**
 * Git/PR status bar for the session's workspace, rendered above the composer.
 *
 * Collapsed it is a one-line summary (branch, diff size, ahead/behind, PR
 * badge); expanded it lists the branch's pull requests with their CI state.
 *
 * @param sessionId Session/conversation id. Nullish renders nothing.
 */
export function GitWorkspaceStatusWidget({ sessionId }: { sessionId: string | null | undefined }) {
  const [open, setOpen] = useState(false);
  const { data } = useGitPrStatus(sessionId);

  if (!isGitWorkspaceStatusVisible(data) || !data) return null;

  const branch = data.branch as string;
  const diff = data.diff;
  const ahead = data.ahead ?? 0;
  const behind = data.behind ?? 0;
  const pr = openPullRequest(data.prs);
  const compare = pr ? null : compareUrl(data);
  // A change set can have files but no line counts (binary files, renames,
  // mode changes) — "+0 -0" would read as "nothing changed", so name the
  // files instead. Keeps the summary honest with the visibility rule.
  const diffSummary = !diff ? null : diff.added + diff.removed > 0 ? (
    <>
      <span className="text-success">+{diff.added}</span>{" "}
      <span className="text-destructive">-{diff.removed}</span>
    </>
  ) : diff.files > 0 ? (
    `${diff.files} arquivo(s)`
  ) : null;

  return (
    <div className="chat-git-status px-4 md:px-6">
      <Collapsible
        open={open}
        onOpenChange={setOpen}
        data-testid="git-workspace-status"
        className={cn(
          "mx-auto mb-1.5 w-full rounded-2xl border border-border bg-card shadow-sm",
          COLUMN_WIDTH,
        )}
      >
        <div className="flex items-start gap-2 px-3 py-1.5 text-xs">
          {/* Wraps instead of overflowing: on a narrow composer the summary
              items fall to a second line rather than pushing the PR link off
              the card. The branch owns the flexible slot and truncates. */}
          <CollapsibleTrigger
            className="flex min-w-0 flex-1 flex-wrap items-center gap-x-2 gap-y-1 text-left text-muted-foreground hover:text-foreground"
            aria-label="Detalhes do workspace"
          >
            <ChevronRightIcon
              className={cn("size-3.5 shrink-0 transition-transform", open && "rotate-90")}
            />
            <GitBranchIcon className="size-3.5 shrink-0" />
            <span data-testid="git-status-branch" className="min-w-0 truncate" title={branch}>
              {branch}
            </span>
            {diffSummary && (
              <span data-testid="git-status-diff" className="shrink-0 font-mono">
                {diffSummary}
              </span>
            )}
            {ahead > 0 && <span className="shrink-0">↑{ahead}</span>}
            {behind > 0 && <span className="shrink-0">↓{behind}</span>}
            {pr && (
              <Badge variant="outline" className="shrink-0 gap-1">
                <GitPullRequestIcon />#{pr.number}
                <CiIndicator status={pr.ci_status} />
              </Badge>
            )}
          </CollapsibleTrigger>
          {pr ? (
            <a
              data-testid="git-status-pr-link"
              href={pr.url}
              target="_blank"
              rel="noopener noreferrer"
              className="shrink-0 font-medium text-primary hover:underline"
            >
              Ver PR #{pr.number}
            </a>
          ) : (
            compare && (
              <a
                data-testid="git-status-create-pr"
                href={compare}
                target="_blank"
                rel="noopener noreferrer"
                className="shrink-0 font-medium text-primary hover:underline"
              >
                Criar PR
              </a>
            )
          )}
        </div>
        <CollapsibleContent>
          <div className="space-y-1 border-t border-border px-3 py-2 text-xs text-muted-foreground">
            {data.base_branch && (
              <div data-testid="git-status-base">
                Base: <span className="font-mono">{data.base_branch}</span>
                {diff ? ` · ${diff.files} arquivo(s)` : ""}
              </div>
            )}
            {data.prs.length === 0 ? (
              <div>Nenhum pull request para este branch.</div>
            ) : (
              data.prs.map((item) => (
                <a
                  key={item.number}
                  href={item.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  data-testid={`git-status-pr-${item.number}`}
                  className="flex flex-wrap items-center gap-x-2 gap-y-1 hover:text-foreground"
                >
                  <GitPullRequestIcon className="size-3.5 shrink-0" />
                  <span className="shrink-0">#{item.number}</span>
                  <span className="min-w-0 truncate">{item.title}</span>
                  <span className="shrink-0">({PR_STATE_LABEL[item.state]})</span>
                  <CiIndicator status={item.ci_status} showLabel />
                </a>
              ))
            )}
          </div>
        </CollapsibleContent>
      </Collapsible>
    </div>
  );
}
