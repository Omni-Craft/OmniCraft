// Collapsible git/PR status bar anchored above the composer.
//
// Contextual: it only appears when the session's workspace actually has
// something to report — uncommitted/committed changes, commits ahead of the
// base, or a pull request for the branch. A clean tree with no PR shows
// nothing, and a git failure hides the bar rather than putting an error in
// front of the chat.

import { ChevronRightIcon, GitBranchIcon, GitPullRequestIcon, Loader2Icon } from "lucide-react";
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

/**
 * GitHub compare URL for opening a PR from `branch` onto the base.
 *
 * The endpoint carries no repo slug, so it is recovered from a PR URL when
 * the branch has one. Without a slug (the common no-PR case) there is
 * nothing to link to and the caller renders no button.
 */
export function compareUrl(status: GitPrStatus): string | null {
  const base = baseBranchName(status.base_branch);
  if (!base || !status.branch) return null;
  for (const pr of status.prs) {
    const match = /^https:\/\/github\.com\/([^/]+\/[^/]+)\/pull\/\d+/.exec(pr.url);
    if (match) {
      return `https://github.com/${match[1]}/compare/${encodeURIComponent(base)}...${encodeURIComponent(status.branch)}?expand=1`;
    }
  }
  return null;
}

/** CI dot for a PR: spinner while pending, a colored bullet once settled. */
function CiIndicator({ status }: { status: GitPullRequest["ci_status"] }) {
  if (status === null) return null;
  if (status === "pending") {
    return <Loader2Icon data-testid="ci-pending" className="size-3 shrink-0 animate-spin" />;
  }
  return (
    <span
      data-testid={`ci-${status}`}
      aria-hidden="true"
      className={cn(
        "size-1.5 shrink-0 rounded-full",
        status === "success" ? "bg-success" : "bg-destructive",
      )}
    />
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
        <div className="flex items-center gap-2 px-3 py-1.5 text-xs">
          <CollapsibleTrigger
            className="flex min-w-0 flex-1 items-center gap-2 text-muted-foreground hover:text-foreground"
            aria-label="Detalhes do workspace"
          >
            <ChevronRightIcon
              className={cn("size-3.5 shrink-0 transition-transform", open && "rotate-90")}
            />
            <GitBranchIcon className="size-3.5 shrink-0" />
            <span data-testid="git-status-branch" className="min-w-0 truncate" title={branch}>
              {branch}
            </span>
            {diff && diff.added + diff.removed > 0 && (
              <span data-testid="git-status-diff" className="shrink-0 font-mono">
                <span className="text-success">+{diff.added}</span>{" "}
                <span className="text-destructive">-{diff.removed}</span>
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
              rel="noreferrer"
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
                rel="noreferrer"
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
                  rel="noreferrer"
                  data-testid={`git-status-pr-${item.number}`}
                  className="flex items-center gap-2 hover:text-foreground"
                >
                  <GitPullRequestIcon className="size-3.5 shrink-0" />
                  <span className="shrink-0">#{item.number}</span>
                  <span className="min-w-0 truncate">{item.title}</span>
                  <span className="shrink-0">({PR_STATE_LABEL[item.state]})</span>
                  <CiIndicator status={item.ci_status} />
                </a>
              ))
            )}
          </div>
        </CollapsibleContent>
      </Collapsible>
    </div>
  );
}
