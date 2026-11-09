The user has a follow-up request for a session that already has a plan. Add the tasks needed for the follow-up.

<existing_tasks>
$existing_json
</existing_tasks>

<follow_up>
$follow_up
</follow_up>

Agents available: $agent_count. This is a parallelism hint only: never add, split or duplicate tasks to match it.
Tools the agents have: $tools
Constraints:
$constraints

Rules for follow-ups:
- Return **only the new tasks** in `tasks`. Do not repeat or change existing tasks; they stay as they are.
- Add at most $max_new tasks.
- Give new tasks new keys, starting at $first_key. Never reuse an existing key.
- New tasks may depend on existing tasks (prefer tasks with status `done`) and use their declared outputs as `inputs`. Never depend on a task with status `failed` or `cancelled`.
- `rationale`, `assumptions` and `questions_for_user` are about the follow-up.

Reply with the JSON object only.
