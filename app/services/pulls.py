from __future__ import annotations

from app.github.client import GitHubClient
from app.models.pulls import (
    AddLabelsRequest,
    AddLabelsResponse,
    CommentPullRequestRequest,
    CommentPullRequestResponse,
    CreatePullRequestRequest,
    CreatePullRequestResponse,
    GetPullRequestRequest,
    GetPullRequestResponse,
    ListPullRequestsRequest,
    ListPullRequestsResponse,
    MergePullRequestRequest,
    MergePullRequestResponse,
    PullRequestFile,
    PullRequestFilesRequest,
    PullRequestFilesResponse,
    PullRequestInfo,
    RequestReviewersRequest,
    RequestReviewersResponse,
    UpdatePullRequestRequest,
    UpdatePullRequestResponse,
)
from app.policy.rules import Policy


class PullRequestService:
    def __init__(self, github: GitHubClient, policy: Policy) -> None:
        self.github = github
        self.policy = policy

    async def create_pull_request(self, owner: str, repo: str, request: CreatePullRequestRequest) -> CreatePullRequestResponse:
        self.policy.assert_repo_allowed(owner, repo)
        self.policy.assert_write_branch_allowed(request.head_branch)
        self.policy.assert_base_branch_allowed(request.base_branch)

        existing = await self.github.list_pull_requests(owner, repo, head=f"{owner}:{request.head_branch}", base=request.base_branch, state="open")
        if existing:
            pr = existing[0]
            return CreatePullRequestResponse(
                pr_number=pr["number"],
                pr_url=pr["html_url"],
                state=pr["state"],
                head_sha=pr["head"]["sha"],
                base_branch=pr["base"]["ref"],
                already_exists=True,
            )

        pr = await self.github.create_pull_request(
            owner,
            repo,
            head=request.head_branch,
            base=request.base_branch,
            title=request.title,
            body=request.body,
        )
        return CreatePullRequestResponse(
            pr_number=pr["number"],
            pr_url=pr["html_url"],
            state=pr["state"],
            head_sha=pr["head"]["sha"],
            base_branch=pr["base"]["ref"],
            already_exists=False,
        )

    async def get_pull_request(self, owner: str, repo: str, request: GetPullRequestRequest) -> GetPullRequestResponse:
        self.policy.assert_repo_allowed(owner, repo)
        pr = await self.github.get_pull_request(owner, repo, request.pr_number)
        return GetPullRequestResponse(pull_request=self._pull_info(pr))

    async def list_pull_requests(self, owner: str, repo: str, request: ListPullRequestsRequest) -> ListPullRequestsResponse:
        self.policy.assert_repo_allowed(owner, repo)
        head = None
        if request.head_branch:
            head = request.head_branch if ":" in request.head_branch else f"{owner}:{request.head_branch}"
        prs = await self.github.list_pull_requests(
            owner,
            repo,
            head=head,
            base=request.base_branch,
            state=request.state,
        )
        items = [self._pull_info(pr) for pr in prs[: request.max_results]]
        return ListPullRequestsResponse(pull_requests=items, total_count=len(items))

    async def get_pull_request_files(self, owner: str, repo: str, request: PullRequestFilesRequest) -> PullRequestFilesResponse:
        self.policy.assert_repo_allowed(owner, repo)
        files = await self.github.get_pull_request_files(owner, repo, request.pr_number, per_page=request.max_results)
        mapped = [
            PullRequestFile(
                filename=item.get("filename", ""),
                status=item.get("status", ""),
                operation=item.get("status", ""),
                additions=int(item.get("additions") or 0),
                deletions=int(item.get("deletions") or 0),
                changes=int(item.get("changes") or 0),
                previous_filename=item.get("previous_filename"),
                patch=item.get("patch"),
                sha=item.get("sha"),
                blob_url=item.get("blob_url"),
                raw_url=item.get("raw_url"),
            )
            for item in files[: request.max_results]
        ]
        return PullRequestFilesResponse(pr_number=request.pr_number, files=mapped, total_count=len(mapped))

    async def update_pull_request(self, owner: str, repo: str, request: UpdatePullRequestRequest) -> UpdatePullRequestResponse:
        self.policy.assert_repo_allowed(owner, repo)
        original = await self.github.get_pull_request(owner, repo, request.pr_number)
        self._assert_pr_mutation_allowed(original)
        if request.base_branch:
            self.policy.assert_base_branch_allowed(request.base_branch)
        updated = await self.github.update_pull_request(
            owner,
            repo,
            request.pr_number,
            title=request.title,
            body=request.body,
            state=request.state,
            base=request.base_branch,
        )
        return UpdatePullRequestResponse(pull_request=self._pull_info(updated))

    async def comment_pull_request(self, owner: str, repo: str, request: CommentPullRequestRequest) -> CommentPullRequestResponse:
        self.policy.assert_repo_allowed(owner, repo)
        pr = await self.github.get_pull_request(owner, repo, request.pr_number)
        self._assert_pr_mutation_allowed(pr)
        comment = await self.github.create_issue_comment(owner, repo, request.pr_number, request.body)
        return CommentPullRequestResponse(
            comment_id=comment["id"],
            comment_url=comment.get("html_url") or comment.get("url") or "",
            body=comment.get("body", ""),
            created_at=comment.get("created_at"),
        )

    async def request_reviewers(self, owner: str, repo: str, request: RequestReviewersRequest) -> RequestReviewersResponse:
        self.policy.assert_repo_allowed(owner, repo)
        pr = await self.github.get_pull_request(owner, repo, request.pr_number)
        self._assert_pr_mutation_allowed(pr)
        if hasattr(self.github, "request_pull_request_reviewers"):
            payload = await self.github.request_pull_request_reviewers(owner, repo, request.pr_number, reviewers=request.reviewers, team_reviewers=request.team_reviewers)
        else:
            payload = await self.github.request_pull_reviewers(owner, repo, request.pr_number, request.reviewers, request.team_reviewers)
        reviewers = [item.get("login", "") for item in payload.get("requested_reviewers", [])]
        teams = [item.get("slug", "") for item in payload.get("requested_teams", [])]
        return RequestReviewersResponse(pr_number=request.pr_number, requested_reviewers=reviewers, requested_team_reviewers=teams, requested_teams=teams)

    async def add_labels(self, owner: str, repo: str, request: AddLabelsRequest) -> AddLabelsResponse:
        self.policy.assert_repo_allowed(owner, repo)
        pr = await self.github.get_pull_request(owner, repo, request.pr_number)
        self._assert_pr_mutation_allowed(pr)
        labels = await self.github.add_issue_labels(owner, repo, request.pr_number, request.labels)
        label_names = [item.get("name", "") for item in labels]
        return AddLabelsResponse(pr_number=request.pr_number, labels=label_names, raw=labels)

    async def merge_pull_request(self, owner: str, repo: str, request: MergePullRequestRequest) -> MergePullRequestResponse:
        self.policy.assert_repo_allowed(owner, repo)
        self.policy.assert_auto_merge_allowed()

        pr = await self.github.get_pull_request(owner, repo, request.pr_number)
        self.policy.assert_write_branch_allowed(pr["head"]["ref"])
        self.policy.assert_base_branch_allowed(pr["base"]["ref"])

        merged = await self.github.merge_pull_request(
            owner,
            repo,
            request.pr_number,
            merge_method=request.merge_method,
            commit_title=request.commit_title,
            commit_message=request.commit_message,
        )
        return MergePullRequestResponse(
            merged=bool(merged.get("merged")),
            message=str(merged.get("message", "")),
            sha=merged.get("sha"),
        )

    def _assert_pr_mutation_allowed(self, pr: dict) -> None:
        self.policy.assert_write_branch_allowed(pr["head"]["ref"])
        self.policy.assert_base_branch_allowed(pr["base"]["ref"])

    @staticmethod
    def _pull_info(pr: dict) -> PullRequestInfo:
        labels = []
        for label in pr.get("labels") or []:
            if isinstance(label, dict):
                labels.append(str(label.get("name", "")))
            else:
                labels.append(str(label))
        return PullRequestInfo(
            pr_number=pr["number"],
            pr_url=pr.get("html_url") or pr.get("url") or "",
            state=pr.get("state", ""),
            title=pr.get("title", ""),
            body=pr.get("body"),
            head_branch=pr.get("head", {}).get("ref", ""),
            head_sha=pr.get("head", {}).get("sha", ""),
            base_branch=pr.get("base", {}).get("ref", ""),
            base_sha=pr.get("base", {}).get("sha"),
            merged=pr.get("merged"),
            draft=pr.get("draft"),
            mergeable=pr.get("mergeable"),
            labels=labels,
            created_at=pr.get("created_at"),
            updated_at=pr.get("updated_at"),
        )
