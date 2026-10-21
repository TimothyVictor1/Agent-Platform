"""initial schema (SPEC §17)

Revision ID: 0001
Revises:
Create Date: 2026-10-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())


def _session_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["session_id"],
        ["sessions.id"],
        name=f"fk_{table}_session_id_sessions",
        ondelete="CASCADE",
    )


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "users",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
    )
    # v1 is single-user: every session belongs to the "local" user.
    op.execute("INSERT INTO users (id, name) VALUES ('local', 'Local user')")

    op.create_table(
        "sessions",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("agent_count", sa.Integer(), nullable=False),
        sa.Column("model_profile", sa.String(100), nullable=False),
        sa.Column("sandbox_provider", sa.String(32), nullable=False),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("plan_version", sa.Integer(), nullable=False),
        sa.Column("budgets", JSONB, nullable=False),
        sa.Column("usage", JSONB, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("started_at", TS, nullable=True),
        sa.Column("ended_at", TS, nullable=True),
        sa.Column("runtime_owner", sa.String(200), nullable=True),
        sa.Column("runtime_lease_expires_at", TS, nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_sessions_user_id_users"),
        sa.PrimaryKeyConstraint("id", name="pk_sessions"),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_index("ix_sessions_status", "sessions", ["status"])

    op.create_table(
        "session_counters",
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("next_seq", sa.BigInteger(), nullable=False),
        _session_fk("session_counters"),
        sa.PrimaryKeyConstraint("session_id", name="pk_session_counters"),
    )

    op.create_table(
        "events",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("ts", TS, nullable=False),
        sa.Column("actor", JSONB, nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("causation_id", sa.String(36), nullable=True),
        sa.Column("correlation_id", sa.String(64), nullable=True),
        _session_fk("events"),
        sa.PrimaryKeyConstraint("id", name="pk_events"),
        sa.UniqueConstraint("session_id", "seq", name="uq_events_session_id_seq"),
    )
    op.create_index("ix_events_session_id_type", "events", ["session_id", "type"])

    op.create_table(
        "plans",
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("proposal", JSONB, nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("approved_at", TS, nullable=True),
        _session_fk("plans"),
        sa.PrimaryKeyConstraint("session_id", "version", name="pk_plans"),
    )

    op.create_table(
        "tasks",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("plan_version", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(16), nullable=False),
        sa.Column("title", sa.String(80), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("acceptance_criteria", JSONB, nullable=False),
        sa.Column("depends_on", JSONB, nullable=False),
        sa.Column("inputs", JSONB, nullable=False),
        sa.Column("outputs", JSONB, nullable=False),
        sa.Column("tags", JSONB, nullable=False),
        sa.Column("suggested_tools", JSONB, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("assigned_agent_id", sa.String(36), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("rework_count", sa.Integer(), nullable=False),
        sa.Column("result", JSONB, nullable=True),
        sa.Column("created_by", sa.String(32), nullable=False),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TS, server_default=sa.func.now(), nullable=False),
        _session_fk("tasks"),
        sa.PrimaryKeyConstraint("id", name="pk_tasks"),
        sa.UniqueConstraint("session_id", "key", name="uq_tasks_session_id_key"),
    )
    op.create_index("ix_tasks_session_id", "tasks", ["session_id"])

    op.create_table(
        "agents",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(40), nullable=False),
        sa.Column("color", sa.String(7), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("sandbox", JSONB, nullable=True),
        sa.Column("current_task_id", sa.String(36), nullable=True),
        sa.Column("step", sa.Integer(), nullable=False),
        sa.Column("stream_url", sa.Text(), nullable=True),
        sa.Column("interactive_stream_url", sa.Text(), nullable=True),
        sa.Column("last_heartbeat_at", TS, nullable=True),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        _session_fk("agents"),
        sa.PrimaryKeyConstraint("id", name="pk_agents"),
        sa.UniqueConstraint("session_id", "name", name="uq_agents_session_id_name"),
    )
    op.create_index("ix_agents_session_id", "agents", ["session_id"])

    op.create_table(
        "board_entries",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("author", JSONB, nullable=False),
        sa.Column("author_type", sa.String(16), nullable=False),
        sa.Column("author_id", sa.String(64), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("tags", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("task_key", sa.String(16), nullable=True),
        sa.Column("refs", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("to_agents", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("status", sa.String(16), nullable=True),
        sa.Column("artifact_id", sa.String(36), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column(
            "search",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('english', coalesce(title, '') || ' ' || coalesce(body, ''))",
                persisted=True,
            ),
            nullable=False,
        ),
        _session_fk("board_entries"),
        sa.PrimaryKeyConstraint("id", name="pk_board_entries"),
        sa.UniqueConstraint("session_id", "seq", name="uq_board_entries_session_id_seq"),
    )
    op.create_index(
        "ix_board_entries_search", "board_entries", ["search"], postgresql_using="gin"
    )
    op.create_index(
        "ix_board_entries_title_trgm",
        "board_entries",
        ["title"],
        postgresql_using="gin",
        postgresql_ops={"title": "gin_trgm_ops"},
    )
    op.create_index("ix_board_entries_tags", "board_entries", ["tags"], postgresql_using="gin")
    op.create_index(
        "ix_board_entries_session_id_kind_status",
        "board_entries",
        ["session_id", "kind", "status"],
    )

    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("mime", sa.String(255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("produced_by", JSONB, nullable=False),
        sa.Column("task_key", sa.String(16), nullable=True),
        sa.Column("source_path", sa.Text(), nullable=True),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        _session_fk("artifacts"),
        sa.PrimaryKeyConstraint("id", name="pk_artifacts"),
        sa.UniqueConstraint(
            "session_id", "name", "version", name="uq_artifacts_session_id_name_version"
        ),
    )
    op.create_index("ix_artifacts_session_id", "artifacts", ["session_id"])

    op.create_table(
        "approvals",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("agent_id", sa.String(36), nullable=False),
        sa.Column("task_key", sa.String(16), nullable=True),
        sa.Column("action_summary", sa.Text(), nullable=False),
        sa.Column("risk", sa.String(8), nullable=False),
        sa.Column("frame_id", sa.String(36), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("decided_by", JSONB, nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("decided_at", TS, nullable=True),
        sa.Column("expires_at", TS, nullable=True),
        _session_fk("approvals"),
        sa.PrimaryKeyConstraint("id", name="pk_approvals"),
    )
    op.create_index("ix_approvals_session_id_status", "approvals", ["session_id", "status"])

    op.create_table(
        "frames",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("agent_id", sa.String(36), nullable=False),
        sa.Column("step", sa.Integer(), nullable=False),
        sa.Column("task_key", sa.String(16), nullable=True),
        sa.Column("ts", TS, nullable=False),
        sa.Column("storage_key_small", sa.Text(), nullable=False),
        sa.Column("storage_key_full", sa.Text(), nullable=True),
        sa.Column("action_label", sa.String(200), nullable=False),
        sa.Column("phash", sa.String(32), nullable=False),
        _session_fk("frames"),
        sa.PrimaryKeyConstraint("id", name="pk_frames"),
    )
    op.create_index(
        "ix_frames_session_id_agent_id_step", "frames", ["session_id", "agent_id", "step"]
    )


def downgrade() -> None:
    for table in (
        "frames",
        "approvals",
        "artifacts",
        "board_entries",
        "agents",
        "tasks",
        "plans",
        "events",
        "session_counters",
        "sessions",
        "users",
    ):
        op.drop_table(table)
