import type { PlanError, ProblemError, TaskDraft } from "@/contracts/generated";

const object = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);

/** Check JSON Schema vocabulary without resolving references or executing schema content. */
export function schemaError(schema: unknown): string | null {
  if (typeof schema === "boolean") return null;
  if (!object(schema)) return "A JSON schema must be an object or boolean.";
  const types = ["null", "boolean", "object", "array", "number", "string", "integer"];
  for (const [key, value] of Object.entries(schema)) {
    if (key === "type" && !(typeof value === "string" ? types.includes(value) : Array.isArray(value) && value.length > 0 && new Set(value).size === value.length && value.every(v => typeof v === "string" && types.includes(v)))) return "Schema type must name a JSON type (or a unique nonempty list of types).";
    if (["$id", "$schema", "$ref", "$anchor", "$dynamicRef", "$dynamicAnchor", "$comment", "title", "description", "format", "contentEncoding", "contentMediaType", "pattern"].includes(key) && typeof value !== "string") return `${key} must be a string.`;
    if (["minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"].includes(key) && (typeof value !== "number" || !Number.isFinite(value) || (key === "multipleOf" && value <= 0))) return `${key} must be ${key === "multipleOf" ? "a positive" : "a finite"} number.`;
    if (["minLength", "maxLength", "minItems", "maxItems", "minContains", "maxContains", "minProperties", "maxProperties"].includes(key) && (typeof value !== "number" || !Number.isInteger(value) || value < 0)) return `${key} must be a nonnegative integer.`;
    if (["uniqueItems", "readOnly", "writeOnly", "deprecated"].includes(key) && typeof value !== "boolean") return `${key} must be a boolean.`;
    if (key === "required" && (!Array.isArray(value) || value.some(v => typeof v !== "string") || new Set(value).size !== value.length)) return "required must be a unique list of strings.";
    if (key === "enum" && !Array.isArray(value)) return "enum must be an array.";
    if (key === "examples" && !Array.isArray(value)) return "examples must be an array.";
    if (["allOf", "anyOf", "oneOf", "prefixItems"].includes(key)) {
      if (!Array.isArray(value) || value.length === 0) return `${key} must be a nonempty array of schemas.`;
      for (const child of value) { const error = schemaError(child); if (error) return `${key}: ${error}`; }
    }
    if (["not", "if", "then", "else", "items", "contains", "additionalProperties", "unevaluatedProperties", "unevaluatedItems", "propertyNames", "contentSchema"].includes(key)) {
      const error = schemaError(value); if (error) return `${key}: ${error}`;
    }
    if (["$defs", "definitions", "properties", "patternProperties", "dependentSchemas"].includes(key)) {
      if (!object(value)) return `${key} must be an object of schemas.`;
      for (const child of Object.values(value)) { const error = schemaError(child); if (error) return `${key}: ${error}`; }
    }
    if (key === "dependentRequired") {
      if (!object(value) || Object.values(value).some(v => !Array.isArray(v) || v.some(s => typeof s !== "string") || new Set(v).size !== v.length)) return "dependentRequired must contain unique lists of strings.";
    }
    if (key === "$vocabulary" && (!object(value) || Object.values(value).some(v => typeof v !== "boolean"))) return "$vocabulary must contain booleans.";
  }
  return null;
}

/** Cross-task checks follow contracts/plan.py::validate_plan, including cycle paths. */
export function validatePlan(tasks: TaskDraft[]): PlanError[] {
  const errors: PlanError[] = [];
  const add = (code: PlanError["code"], message: string, task_key: string | null = null, field: string | null = null, cycle: string[] | null = null) => errors.push({ code, message, task_key, field, cycle });
  if (!tasks.length) { add("empty_plan", "The plan has no tasks."); return errors; }
  if (tasks.length > 12) add("too_many_tasks", "The plan may have at most 12 tasks.");
  const byKey = new Map<string, TaskDraft>();
  for (const task of tasks) {
    if (byKey.has(task.key)) add("duplicate_key", `Task key ${task.key} is used more than once.`, task.key, "key");
    else byKey.set(task.key, task);
  }
  for (const task of tasks) {
    const local = (code: PlanError["code"], message: string, field: string) => add(code, message, task.key, field);
    if (!task.acceptance_criteria.length) local("empty_criteria", "Add at least one acceptance criterion.", "acceptance_criteria");
    if (task.acceptance_criteria.length > 6) local("too_many_criteria", "Use at most six acceptance criteria.", "acceptance_criteria");
    if (task.acceptance_criteria.some(c => !c.trim())) local("blank_criterion", "Acceptance criteria cannot be blank.", "acceptance_criteria");
    const outputs = new Set<string>();
    for (const output of task.outputs) {
      if (outputs.has(output.key)) local("duplicate_output_key", `Output ${output.key} is declared twice.`, "outputs");
      outputs.add(output.key);
      if (output.json_schema !== null) {
        if (output.kind !== "json") local("json_schema_on_non_json", `Only JSON outputs can have a schema (${output.key}).`, "outputs");
        else { const error = schemaError(output.json_schema); if (error) local("invalid_json_schema", `${output.key}: ${error}`, "outputs"); }
      }
    }
    for (const dep of task.depends_on) {
      if (dep === task.key) local("self_dependency", "A task cannot depend on itself.", "depends_on");
      else if (!byKey.has(dep)) local("unknown_dependency", `Unknown dependency: ${dep}.`, "depends_on");
    }
    for (const input of task.inputs) {
      const upstream = byKey.get(input.from_task);
      if (!upstream) { local("unknown_input_task", `Unknown input task: ${input.from_task}.`, "inputs"); continue; }
      if (!task.depends_on.includes(input.from_task)) local("input_not_dependency", `Input task ${input.from_task} must be a dependency.`, "inputs");
      if (!upstream.outputs.some(o => o.key === input.output_key)) local("unknown_input_output", `Unknown output ${input.from_task}.${input.output_key}.`, "inputs");
    }
  }
  const colors = new Map<string, number>();
  const stack: string[] = [];
  const seen = new Set<string>();
  function visit(key: string) {
    colors.set(key, 1); stack.push(key);
    for (const dep of byKey.get(key)!.depends_on) {
      if (!byKey.has(dep) || dep === key) continue;
      if (colors.get(dep) === 1) {
        const cycle = [...stack.slice(stack.indexOf(dep)), dep];
        const identity = [...new Set(cycle)].sort().join(",");
        if (!seen.has(identity)) { seen.add(identity); add("cycle", `Dependency cycle: ${cycle.join(" → ")}.`, dep, "depends_on", cycle); }
      } else if (!colors.has(dep)) visit(dep);
    }
    stack.pop(); colors.set(key, 2);
  }
  for (const key of byKey.keys()) if (!colors.has(key)) visit(key);
  return errors;
}

export function fieldErrors(tasks: TaskDraft[]): ProblemError[] {
  return tasks.flatMap(task => {
    const errors: ProblemError[] = [];
    const add = (message: string) => errors.push({ message, task_key: task.key, code: null, loc: null, details: null });
    if (!/^[A-Za-z0-9_-]{1,16}$/.test(task.key)) add("Task keys must be 1–16 letters, numbers, underscores or hyphens.");
    if (!task.title.length || task.title.length > 80) add("Title must have 1–80 characters.");
    if (!task.goal.length || task.goal.length > 1200) add("Goal must have 1–1200 characters.");
    for (const output of task.outputs) {
      if (!/^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(output.key)) add("Output keys must start with a letter or underscore and contain at most 64 letters, numbers or underscores.");
      if (output.description.length > 500) add("Output description must have at most 500 characters.");
    }
    return errors;
  });
}

export function cycleEdges(errors: PlanError[]): Set<string> {
  return new Set(errors.flatMap(error => (error.cycle ?? []).slice(1).map((dep, i) => `${dep}->${error.cycle![i]}`)));
}

export function nodeErrors(key: string, errors: (PlanError | ProblemError)[]): string[] {
  return errors.filter(error => error.task_key === key || ("cycle" in error && error.cycle?.includes(key))).map(error => error.message);
}
