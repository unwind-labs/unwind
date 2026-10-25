"""Claude Code's per-project JSONL source."""

from .base import SessionFile, SessionSource


class ClaudeSource(SessionSource):
    id = "claude"
    label = "Claude Code"

    def discover_projects(self):
        from ..projects import claude_projects_root

        root = claude_projects_root()
        if root.is_dir():
            yield from ((p.name, p) for p in root.iterdir() if p.is_dir())

    def list_sessions(self, slug, project_dir, *, fresh=False):
        from ..projects import project_jsonl_listing

        return tuple(
            SessionFile(e.sid, e.path, e.mtime, e.size, self.id)
            for e in project_jsonl_listing(project_dir, fresh=fresh)
        )

    def owns_session(self, session_id):
        # Other adapters claim their own prefixed namespaces first.
        return True

    def resolve(self, slug, project_dir, session_id):
        path = project_dir / f"{session_id}.jsonl"
        return path if path.is_file() else None

    def matches_record(self, first):
        return True  # Legacy records are the default shared schema.

    def normalize(self, records):
        yield from records

    def estimate_cost(self, model, cw, cr, r, w):
        from ..pricing import cost_usd

        return cost_usd(model, cw, cr, r, w)

    def session_status(self):
        return None
