Revise the current plan according to the user's instruction.

<current_plan>
$plan_json
</current_plan>

<instruction>
$instruction
</instruction>

Agents available: $agent_count. This is a parallelism hint only: never add, split or duplicate tasks to match it.
Tools the agents have: $tools
Constraints:
$constraints

Return the complete new plan (every task, not only the changed ones), following all planning rules.
- Keep the key of every task you keep, even if you change it.
- Give new tasks keys that are not used in the current plan (the next free ones are $next_keys).
- Do not reuse the key of a task you remove for a different task.

Reply with the plan JSON object only.
